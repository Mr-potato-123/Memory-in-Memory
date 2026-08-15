"""Mem0 data-plane integration tests without network access."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mim.artifacts import RunDir
from mim.agents.mem0_native_access import Mem0NativeAccessAgent
from mim.config import MiMConfig, ModelConfig
from mim.llm.mock_client import MockClient
from mim.retrieval.embedder import Embedder
from mim.retrieval.mem0_backend import Mem0Backend
from mim.schemas import (
    Conversation,
    Message,
    ModelResponse,
    Question,
    Session,
    Side,
    SkillRecord,
)
from mim.storage.sqlite_store import SearchFilters
from mim.workflows.use import MiMRuntime


class FakeMem0:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}
        self.add_calls: list[dict] = []
        self.search_calls: list[dict] = []

    def add(self, **kwargs):
        self.add_calls.append(kwargs)
        user_id = kwargs["user_id"]
        metadata = dict(kwargs.get("metadata", {}))
        source_ids = [
            message["source_message_id"]
            for message in kwargs["messages"]
            if message.get("source_message_id")
        ]
        metadata["mim_source_message_ids"] = source_ids
        row = {
            "id": "fake-1",
            "memory": "Alice lives in Seattle.",
            "score": 0.93,
            "metadata": metadata,
        }
        self.rows.setdefault(user_id, []).append(row)
        return {
            "results": [{
                "id": row["id"],
                "memory": row["memory"],
                "event": "ADD",
            }]
        }

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        user_id = kwargs["filters"]["user_id"]
        return {"results": self.rows.get(user_id, [])[:kwargs["top_k"]]}

    def get_all(self, **kwargs):
        filters = kwargs["filters"]
        if "user_id" in filters:
            return {"results": self.rows.get(filters["user_id"], [])}
        clauses = filters.get("AND", [])
        user_id = next(
            (item["user_id"] for item in clauses if "user_id" in item), ""
        )
        session_id = next(
            (item["mim_session_id"] for item in clauses
             if "mim_session_id" in item),
            None,
        )
        rows = self.rows.get(user_id, [])
        if session_id is not None:
            rows = [
                row for row in rows
                if row.get("metadata", {}).get("mim_session_id") == session_id
            ]
        return {"results": rows}


class RecordingModel:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def generate(self, messages, **kwargs):
        self.calls.append(messages)
        return ModelResponse(
            text="Seattle",
            provider="mock",
            model="recording-model",
        )


def _conversation() -> Conversation:
    return Conversation(
        conversation_id="conv_mem0",
        sessions=[Session(
            session_id="conv_mem0_s01",
            time="2024-01-01",
            messages=[Message(
                message_id="conv_mem0:D1:1",
                role="user",
                speaker="Alice",
                content="I live in Seattle.",
                time="2024-01-01",
            )],
        )],
    )


def test_mem0_adapter_normalizes_search_contract():
    fake = FakeMem0()
    backend = Mem0Backend(
        client=fake, namespace="snapshot-a", threshold=0.1, rerank=True
    )
    backend.add_session(
        conversation_id="conv_mem0",
        session_id="conv_mem0_s01",
        messages=[{
            "message_id": "conv_mem0:D1:1",
            "role": "user",
            "speaker": "Alice",
            "content": "I live in Seattle.",
        }],
        session_time="2024-01-01",
    )
    hits = backend.search(
        conversation_id="conv_mem0",
        snapshot_commit_id=1,
        query="Where does Alice live?",
        filters=SearchFilters(
            conversation_id="conv_mem0", entities=["Alice"]
        ),
        top_k=5,
        keywords=["Seattle"],
    )

    assert hits[0].version_id == "mem0:fake-1"
    assert hits[0].content == "Alice lives in Seattle."
    assert hits[0].source_message_ids == ["conv_mem0:D1:1"]
    assert fake.add_calls[0]["messages"][0]["source_message_id"] == (
        "conv_mem0:D1:1"
    )
    assert fake.add_calls[0]["metadata"]["mim_session_source_message_ids"] == [
        "conv_mem0:D1:1"
    ]
    assert fake.search_calls[0]["filters"] == {
        "user_id": "snapshot-a:conv_mem0"
    }
    assert "Exact anchors" in fake.search_calls[0]["query"]


def test_runtime_uses_mem0_as_fact_source_and_sqlite_as_trace_ledger(
    tmp_path: Path,
):
    cfg = MiMConfig(models={
        "runtime": ModelConfig(provider="mock", model="mock-runtime"),
        "maintenance": ModelConfig(provider="mock", model="mock-maintenance"),
    })
    cfg.storage.backend = "mem0"
    cfg.access.mode = "mem0_native"
    cfg.access.initial_top_k = 20
    cfg.embedding.model = "deterministic-hash"
    model = MockClient(cfg.models["runtime"])
    model.set_script([model._make_resp("Seattle")])
    run_dir = RunDir.create("mem0_runtime", tmp_path)
    runtime = MiMRuntime(
        cfg,
        mode="base",
        run_dir=run_dir,
        runtime_model=model,
        embedder=Embedder("deterministic-hash"),
        mem0_client=FakeMem0(),
    )
    runtime.ingest(_conversation())
    result = runtime.ask(Question(
        qa_id="conv_mem0_qa_0001",
        question="Where does Alice live?",
        reference_answer="Seattle",
        source_evidence=[["conv_mem0_s01", "conv_mem0:D1:1"]],
    ))

    assert result.answer == "Seattle"
    assert result.evidence_ids == []
    assert result.visible_memories[0]["version_id"] == "mem0:fake-1"
    assert "source_message_ids" not in result.visible_memories[0]
    persisted = runtime.store.get_answer_context(result.access_run_id)
    assert persisted[0]["version_id"] == "mem0:fake-1"
    assert persisted[0]["rendered_text"].endswith("Alice lives in Seattle.")


def test_mem0_namespace_ingestion_is_idempotent(tmp_path: Path):
    cfg = MiMConfig(models={
        "runtime": ModelConfig(provider="mock", model="mock-runtime"),
        "maintenance": ModelConfig(provider="mock", model="mock-maintenance"),
    })
    cfg.storage.backend = "mem0"
    cfg.storage.mem0_namespace = "frozen-snapshot"
    cfg.embedding.model = "deterministic-hash"
    fake = FakeMem0()
    runtime = MiMRuntime(
        cfg,
        mode="base",
        run_dir=RunDir.create("mem0_idempotent", tmp_path),
        runtime_model=MockClient(cfg.models["runtime"]),
        embedder=Embedder("deterministic-hash"),
        mem0_client=fake,
    )

    runtime.ingest(_conversation())
    runtime.ingest(_conversation())

    assert len(fake.add_calls) == 1
    assert fake.add_calls[0]["user_id"] == "frozen-snapshot:conv_mem0"


def test_mem0_native_access_routes_skill_after_search_without_retrieving_again():
    fake = FakeMem0()
    backend = Mem0Backend(client=fake)
    backend.add_session(
        conversation_id="conv_mem0",
        session_id="conv_mem0_s01",
        messages=[{
            "message_id": "conv_mem0:D1:1",
            "role": "user",
            "speaker": "Alice",
            "content": "I live in Seattle.",
        }],
        session_time="2024-01-01",
    )
    model = RecordingModel()
    agent = Mem0NativeAccessAgent(model=model, retriever=backend, top_k=20)
    loader_contexts: list[dict] = []
    skill = SkillRecord(
        skill_id="sk_answer_location",
        version=2,
        side=Side.ACCESS,
        name="Verify entity attribution",
        description="Use for a location question about a named person.",
        content=["Answer only from a memory about the named person."],
    )

    def load_after_search(context: dict):
        loader_contexts.append(context)
        assert len(fake.search_calls) == 1
        assert context["first_search"]["hit_count"] == 1
        assert context["first_search"]["hits"][0]["content"] == (
            "Alice lives in Seattle."
        )
        return [skill]

    result = agent.answer(
        question=Question(
            qa_id="qa_location",
            question="Where does Alice live?",
            reference_answer="Seattle",
        ),
        conversation_id="conv_mem0",
        snapshot_commit_id=1,
        skills=[],
        recovery_skill_loader=load_after_search,
    )

    assert len(loader_contexts) == 1
    assert len(fake.search_calls) == 1
    assert result.used_skill_ids == ["sk_answer_location_v2"]
    system_prompt = model.calls[0][0]["content"]
    assert "Verify entity attribution" in system_prompt
    assert "The memory search is already complete" in system_prompt

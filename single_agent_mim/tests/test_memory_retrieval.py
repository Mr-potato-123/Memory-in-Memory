"""Construction and multi-route memory retrieval regression tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mim.agents.access import AccessAgent, EvidenceWorkspace
from mim.agents.construction import ConstructionAgent
from mim.config import ModelConfig
from mim.llm.mock_client import MockClient
from mim.retrieval.embedder import Embedder
from mim.retrieval.hybrid import HybridRetriever
from mim.schemas import AgentAction, Question, Side, SkillRecord
from mim.storage.sqlite_store import (
    MemoryCandidate,
    MemoryHit,
    SearchFilters,
    SQLiteMemoryStore,
)


def _components(tmp_path: Path):
    embedder = Embedder("deterministic-hash")
    store = SQLiteMemoryStore(
        tmp_path / "memory.sqlite3",
        embedding_dim=embedder.dim,
        embedding_model=embedder.model_name,
    )
    store.ensure_conversation("conv")
    return store, embedder, HybridRetriever(store, embedder)


def _insert(
    store: SQLiteMemoryStore,
    embedder: Embedder,
    number: int,
    content: str,
    *,
    subject: str,
    kind: str = "event",
    entities: list[str] | None = None,
    world_start: str | None = None,
) -> None:
    store.insert_memory_version(
        memory_id=f"mem_conv_{number:04d}",
        version_no=1,
        conversation_id="conv",
        memory_kind=kind,
        subject=subject,
        predicate=None,
        object_text=None,
        content=content,
        source_message_ids=[],
        entities=entities or [subject],
        keywords=[],
        embedding=embedder.encode([content])[0],
        system_from_commit=0,
        world_start=world_start,
    )


def test_semantic_route_uses_the_requested_conversation(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "Alice enjoys ceramic painting.",
        subject="Alice",
    )

    hits = retriever.search(
        conversation_id="conv",
        snapshot_commit_id=0,
        query="Alice enjoys ceramic painting",
        strategy="semantic",
        top_k=3,
    )

    assert [hit.memory_id for hit in hits] == ["mem_conv_0001"]
    assert hits[0].matched_paths == ["semantic"]


def test_bm25_and_keyword_routes_are_independently_available(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "James visited Nuuk during his trip.",
        subject="James",
        entities=["James", "Nuuk"],
    )
    _insert(
        store,
        embedder,
        2,
        "James visited a city during his trip.",
        subject="James",
        entities=["James"],
    )

    filters = SearchFilters(conversation_id="conv", as_of_commit=0)
    bm25_hits = retriever.search(
        conversation_id="conv",
        snapshot_commit_id=0,
        query="James Nuuk trip",
        strategy="bm25",
        filters=filters,
        top_k=2,
    )
    keyword_hits = retriever.search(
        conversation_id="conv",
        snapshot_commit_id=0,
        query="additional country",
        keywords=["Nuuk"],
        strategy="keyword",
        filters=filters,
        top_k=2,
    )

    assert bm25_hits[0].memory_id == "mem_conv_0001"
    assert keyword_hits[0].memory_id == "mem_conv_0001"
    assert bm25_hits[0].matched_paths == ["bm25"]
    assert keyword_hits[0].matched_paths == ["keyword"]


def test_access_drops_unknown_memory_kind_filters(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "James bought tickets to Toronto.",
        subject="James",
        kind="plan",
        entities=["James", "Toronto"],
    )
    agent = AccessAgent(
        MockClient(ModelConfig(provider="mock", model="mock")),
        store,
        retriever,
    )
    workspace = EvidenceWorkspace(
        question="Where did James plan to travel?",
        snapshot_commit_id=0,
    )
    observation = agent._execute_search(
        AgentAction(
            action="search_memory",
            arguments={
                "query": "James travel tickets",
                "strategy": "hybrid",
                "keywords": ["Toronto"],
                "memory_kinds": ["travel", "personal"],
                "depth": "deep",
                "top_k": 5,
            },
            reason="Find the destination.",
        ),
        workspace,
        "conv",
    )

    assert observation["memory_kinds"] == []
    assert observation["ignored_memory_kinds"] == ["travel", "personal"]
    assert observation["hits"][0]["memory_id"] == "mem_conv_0001"


def test_access_exposes_world_time_in_search_results(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "John resumed playing drums a month ago.",
        subject="John",
        world_start="2022-02",
    )
    agent = AccessAgent(
        MockClient(ModelConfig(provider="mock", model="mock")),
        store,
        retriever,
    )
    workspace = EvidenceWorkspace(
        question="When did John resume playing drums?",
        snapshot_commit_id=0,
    )

    observation = agent._execute_search(
        AgentAction(
            action="search_memory",
            arguments={
                "query": "John resumed playing drums",
                "strategy": "hybrid",
                "top_k": 3,
            },
            reason="Find the date.",
        ),
        workspace,
        "conv",
    )

    assert observation["hits"][0]["world_start"] == "2022-02"


def test_access_may_answer_after_one_search_when_model_judges_full(
    tmp_path: Path,
):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "James has three dogs.",
        subject="James",
        entities=["James", "dogs"],
    )
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    search = {
        "action": "search_memory",
        "arguments": {
            "strategy": "hybrid",
            "query": "James pets",
            "keywords": ["James", "dogs"],
            "depth": "standard",
            "top_k": 3,
        },
        "reason": "Find the pet count.",
    }
    model.set_script(
        [
            model._make_resp(json.dumps(search)),
            model._make_resp(
                json.dumps(
                    {
                        "action": "answer",
                        "arguments": {
                            "answer": "three",
                            "evidence_version_ids": ["mem_conv_0001_v1"],
                        },
                        "reason": "One memory says three.",
                    }
                )
            ),
        ]
    )
    agent = AccessAgent(
        model,
        store,
        retriever,
        max_steps=4,
    )

    result = agent.answer(
        Question(
            qa_id="qa_count",
            question="How many pets does James have?",
            reference_answer="Three dogs.",
            category=3,
        ),
        conversation_id="conv",
        snapshot_commit_id=0,
        skills=[],
    )

    assert result.answer == "three"
    assert result.steps == 2


def test_access_loads_recovery_skill_only_after_first_default_search(
    tmp_path: Path,
):
    store, embedder, retriever = _components(tmp_path)
    _insert(store, embedder, 1, "James has three dogs.", subject="James")
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    model.set_script([
        model._make_resp(json.dumps({
            "action": "search_memory",
            "arguments": {"strategy": "hybrid", "query": "James pets",
                          "keywords": ["James"], "top_k": 3},
            "reason": "Run the default lookup.",
        })),
        model._make_resp(json.dumps({
            "action": "answer",
            "arguments": {"answer": "three",
                          "evidence_version_ids": ["mem_conv_0001_v1"]},
            "reason": "The first result is complete.",
        })),
    ])
    calls = []
    skill = SkillRecord(
        skill_id="skill_recovery", version=1, side=Side.ACCESS,
        name="Recover a missing count",
        description="When the first result omits the requested count.",
        content=["Search for an explicit quantity."],
    )

    result = AccessAgent(model, store, retriever, max_steps=3).answer(
        Question(qa_id="qa_recovery", question="How many pets?",
                 reference_answer="three", category=1),
        conversation_id="conv", snapshot_commit_id=0, skills=[],
        recovery_skill_loader=lambda context: calls.append(context) or [skill],
    )

    assert len(calls) == 1
    assert calls[0]["first_search"]["hit_count"] == 1
    assert result.used_skill_ids == ["skill_recovery_v1"]


def test_access_normalizes_empty_answer_to_unanswerable(tmp_path: Path):
    store, _, retriever = _components(tmp_path)
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    model.set_script(
        [
            model._make_resp(
                json.dumps(
                    {
                        "action": "answer",
                        "arguments": {
                            "answer": "",
                            "evidence_version_ids": [],
                        },
                        "reason": "The evidence is insufficient.",
                    }
                )
            )
        ]
    )
    agent = AccessAgent(model, store, retriever, max_steps=2)

    result = agent.answer(
        Question(
            qa_id="qa_empty",
            question="What is not present?",
            reference_answer="unknown",
            category=4,
        ),
        conversation_id="conv",
        snapshot_commit_id=0,
        skills=[],
    )

    assert result.answer == "No information available."
    assert result.error is None


def test_access_budget_exhaustion_gets_answer_only_final_turn(tmp_path: Path):
    store, embedder, retriever = _components(tmp_path)
    _insert(
        store,
        embedder,
        1,
        "James has three dogs.",
        subject="James",
    )
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    model.set_script(
        [
            model._make_resp(
                json.dumps(
                    {
                        "action": "search_memory",
                        "arguments": {
                            "strategy": "hybrid",
                            "query": "James dogs",
                            "keywords": ["James", "dogs"],
                            "depth": "standard",
                            "top_k": 3,
                        },
                        "reason": "Retrieve the count.",
                    }
                )
            ),
            model._make_resp(
                json.dumps(
                    {
                        "action": "answer",
                        "arguments": {
                            "answer": "three",
                            "evidence_version_ids": ["mem_conv_0001_v1"],
                        },
                        "reason": "Answer from the accumulated result.",
                    }
                )
            ),
        ]
    )
    agent = AccessAgent(model, store, retriever, max_steps=1)

    result = agent.answer(
        Question(
            qa_id="qa_forced_answer",
            question="How many dogs does James have?",
            reference_answer="three",
            category=1,
        ),
        conversation_id="conv",
        snapshot_commit_id=0,
        skills=[],
    )

    assert result.answer == "three"
    assert result.error is None
    assert result.action_records[-1]["response"]["status"] == (
        "accepted_forced_answer"
    )


class _CountingMock(MockClient):
    def __init__(self):
        super().__init__(ModelConfig(provider="mock", model="mock"))
        self.calls = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        return super().generate(messages, **kwargs)


def test_construction_manager_decides_complete_batch_in_one_call(tmp_path: Path):
    store, embedder, _ = _components(tmp_path)
    model = _CountingMock()
    candidates = [
        MemoryCandidate(
            candidate_id=f"cand_{index}",
            memory_kind="event",
            subject="James",
            predicate=None,
            object_text=None,
            content=content,
            world_start="2022-03-16",
            world_end=None,
            source_message_ids=[f"conv:D1:{index}"],
            entities=["James"],
            keywords=[],
            confidence=0.9,
            embedding=embedder.encode([content])[0],
        )
        for index, content in enumerate(
            ["James went bowling.", "James scored two strikes."], 1
        )
    ]
    model.set_script(
        [
            model._make_resp(
                json.dumps(
                    {
                        "decisions": [
                            {
                                "candidate_id": candidate.candidate_id,
                                "action": "ADD",
                                "update_type": "add",
                                "merged_content": candidate.content,
                                "source_message_ids": (
                                    candidate.source_message_ids
                                ),
                            }
                            for candidate in candidates
                        ]
                    }
                )
            )
        ]
    )
    agent = ConstructionAgent(model, store, embedder)

    plan = agent.build_plan(
        base_commit_id=None,
        conversation_id="conv",
        candidates=candidates,
        skills=[],
    )

    assert model.calls == 1
    assert len(plan.decisions) == 2
    assert all(decision.action == "ADD" for decision in plan.decisions)


def test_construction_crud_gate_rejects_same_topic_but_different_memory(
    tmp_path: Path,
):
    store, embedder, _ = _components(tmp_path)
    agent = ConstructionAgent(
        MockClient(ModelConfig(provider="mock", model="mock")),
        store,
        embedder,
        semantic_crud_threshold=0.88,
    )
    candidate = MemoryCandidate(
        candidate_id="cand_instrument",
        memory_kind="event",
        subject="James",
        predicate="recreation",
        object_text=None,
        content="James is learning to play an instrument.",
        world_start=None,
        world_end=None,
        source_message_ids=["conv:D1:1"],
        entities=["James", "instrument"],
        keywords=["instrument"],
        embedding=embedder.encode(
            ["James is learning to play an instrument."]
        )[0],
    )

    wrong_person = MemoryHit(
        memory_id="mem_john",
        version_id="mem_john_v1",
        subject="John",
        predicate="recreation",
        memory_kind="event",
        content="John enjoys video games.",
        entities=["John", "video games"],
        score=0.99,
        matched_paths=["semantic"],
    )
    broad_same_person = MemoryHit(
        memory_id="mem_james_games",
        version_id="mem_james_games_v1",
        subject="James",
        predicate="recreation",
        memory_kind="event",
        content="James enjoys video games.",
        entities=["James", "video games"],
        score=0.70,
        matched_paths=["key", "semantic"],
    )
    same_logical_event = MemoryHit(
        memory_id="mem_james_instrument",
        version_id="mem_james_instrument_v1",
        subject="James",
        predicate="recreation",
        memory_kind="event",
        content="James recently began music practice.",
        entities=["James", "instrument"],
        score=0.72,
        matched_paths=["key"],
    )

    assert not agent._is_crud_compatible(candidate, wrong_person)
    assert not agent._is_crud_compatible(candidate, broad_same_person)
    assert agent._is_crud_compatible(candidate, same_logical_event)


def test_construction_batch_allows_each_target_only_once(
    tmp_path: Path,
    monkeypatch,
):
    store, embedder, _ = _components(tmp_path)
    model = _CountingMock()
    candidates = [
        MemoryCandidate(
            candidate_id=f"cand_{index}",
            memory_kind="event",
            subject="James",
            predicate="gaming",
            object_text=None,
            content=content,
            world_start=None,
            world_end=None,
            source_message_ids=[f"conv:D1:{index}"],
            entities=["James", "gaming"],
            keywords=["gaming"],
            embedding=embedder.encode([content])[0],
        )
        for index, content in enumerate(
            [
                "James entered an online tournament.",
                "James bought a new gaming system.",
            ],
            1,
        )
    ]
    related = MemoryHit(
        memory_id="mem_james",
        version_id="mem_james_v1",
        subject="James",
        predicate="gaming",
        memory_kind="event",
        content="James enjoys video games.",
        entities=["James", "gaming"],
        score=0.95,
        matched_paths=["semantic"],
    )
    model.set_script(
        [
            model._make_resp(
                json.dumps(
                    {
                        "decisions": [
                            {
                                "candidate_id": candidate.candidate_id,
                                "action": "UPDATE",
                                "target_memory_id": "mem_james",
                                "update_type": "enrichment",
                                "merged_content": candidate.content,
                                "source_message_ids": (
                                    candidate.source_message_ids
                                ),
                            }
                            for candidate in candidates
                        ]
                    }
                )
            )
        ]
    )
    agent = ConstructionAgent(model, store, embedder)
    monkeypatch.setattr(
        agent,
        "_related_memories",
        lambda **_: [related],
    )

    plan = agent.build_plan(
        base_commit_id=0,
        conversation_id="conv",
        candidates=candidates,
        skills=[],
    )

    assert [decision.action for decision in plan.decisions] == [
        "UPDATE",
        "ADD",
    ]
    assert plan.decisions[1].target_memory_id is None
    assert "already claimed" in plan.decisions[1].reason

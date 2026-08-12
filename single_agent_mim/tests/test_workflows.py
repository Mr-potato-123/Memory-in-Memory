"""Integration tests for Runtime, Failure and Skill-Maker workflows."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mim.agents.access_diagnosis import AccessDiagnosisAgent
from mim.agents.access import AccessAgent, SearchChain
from mim.agents.construction_diagnosis import ConstructionDiagnosisAgent
from mim.agents.failure import AnswerCheckAgent
from mim.artifacts import RunDir
from mim.config import MiMConfig, ModelConfig
from mim.eval.locomo import _load_locomo_data
from mim.failure import ProvenanceService
from mim.failure.workflow import FailureWorkflow
from mim.failure.schemas import LearningRoute
from mim.llm.mock_client import MockClient
from mim.retrieval.embedder import Embedder
from mim.schemas import Conversation, Message, Question, Session
from mim.storage.sqlite_store import MemoryHit
from mim.workflows.use import MiMRuntime
from mim.workflows.train import MiMTrainer
from mim.workflows.evaluate import MiMEvaluator


def _config() -> MiMConfig:
    cfg = MiMConfig(
        models={
            "runtime": ModelConfig(provider="mock", model="mock-runtime"),
            "maintenance": ModelConfig(provider="mock", model="mock-maintenance"),
        }
    )
    cfg.embedding.model = "deterministic-hash"
    return cfg


def test_access_normalizes_visible_memory_id_to_version_id():
    chain = SearchChain(question="How many?", snapshot_commit_id=1)
    chain.add_hits([
        MemoryHit(
            memory_id="mem_001",
            version_id="mem_001_v3",
            content="Three children.",
        )
    ])

    assert AccessAgent._normalize_evidence_ids(
        ["mem_001"], chain
    ) == ["mem_001_v3"]


def _conversation() -> Conversation:
    return Conversation(
        conversation_id="conv_test",
        sessions=[Session(
            session_id="conv_test_s01",
            time="2024-01-01",
            messages=[
                Message(
                    message_id="conv_test:D1:1",
                    role="user",
                    speaker="Alice",
                    content="I live in Seattle.",
                    time="2024-01-01",
                )
            ],
        )],
    )


def _runtime_script(model: MockClient, answer: str = "Seattle") -> None:
    model.set_script([
        model._make_resp(json.dumps({"candidates": [{
            "candidate_id": "local",
            "memory_kind": "state",
            "subject": "Alice",
            "predicate": "residence",
            "object_text": "Seattle",
            "content": "Alice lives in Seattle.",
            "world_start": "2024-01-01",
            "world_end": None,
            "source_message_ids": ["conv_test:D1:1"],
            "entities": ["Alice", "Seattle"],
            "keywords": ["Alice", "Seattle", "residence"],
            "importance": 0.8,
            "confidence": 0.95,
        }]})),
        model._make_resp(json.dumps({"decisions": [{
            "candidate_id": "cand_conv_test_conv_test_s01_000",
            "action": "ADD",
            "target_memory_id": None,
            "update_type": "add",
            "reason": "New state.",
            "merged_content": "Alice lives in Seattle.",
            "source_message_ids": ["conv_test:D1:1"],
        }]})),
        model._make_resp(json.dumps({
            "action": "search_memory",
            "arguments": {
                "query": "Alice Seattle residence",
                "strategy": "hybrid",
                "top_k": 5,
            },
            "reason": "Retrieve residence.",
        })),
        model._make_resp(json.dumps({
            "action": "answer",
            "arguments": {
                "answer": answer,
                "evidence_version_ids": ["mem_conv_test_0001_v1"],
                "confidence": 0.9,
            },
            "reason": "Visible memory supports the answer.",
        })),
    ])


def test_runtime_persists_exact_answer_visible_context(tmp_path: Path):
    cfg = _config()
    model = MockClient(cfg.models["runtime"])
    _runtime_script(model)
    run_dir = RunDir.create("runtime_test", tmp_path)
    runtime = MiMRuntime(
        cfg,
        mode="base",
        run_dir=run_dir,
        runtime_model=model,
        embedder=Embedder("deterministic-hash"),
    )
    runtime.ingest(_conversation())
    result = runtime.ask(Question(
        qa_id="conv_test_qa_0001",
        question="Where does Alice live?",
        reference_answer="Seattle",
        source_evidence=[["conv_test_s01", "conv_test:D1:1"]],
    ))

    assert result.answer == "Seattle"
    assert result.evidence_ids == ["mem_conv_test_0001_v1"]
    assert result.answer_prompt_hash
    assert [item["version_id"] for item in result.visible_memories] == [
        "mem_conv_test_0001_v1"
    ]
    assert runtime.store.get_answer_context(result.access_run_id)[0][
        "version_id"
    ] == "mem_conv_test_0001_v1"


def test_train_uses_same_sqlite_runtime_and_selects_empty_bank(tmp_path: Path):
    cfg = _config()
    runtime_model = MockClient(cfg.models["runtime"])
    _runtime_script(runtime_model)
    trainer = MiMTrainer(
        cfg,
        RunDir.create("train_test", tmp_path),
        runtime_model=runtime_model,
        maintenance_model=MockClient(cfg.models["maintenance"]),
        embedder=Embedder("deterministic-hash"),
    )
    question = Question(
        qa_id="conv_test_qa_0001",
        question="Where does Alice live?",
        reference_answer="Seattle",
        source_evidence=[["conv_test_s01", "conv_test:D1:1"]],
    )
    result = trainer.train(
        conversations=[_conversation()],
        questions={"conv_test": [question]},
        train_ids=["conv_test"],
        validation_ids=[],
    )
    assert result.conversations_processed == 1
    assert result.total_qa == 1
    assert result.failures_detected == 0
    assert result.selected_version == 0
    assert (
        tmp_path / "train_test" / "state" / "memory.sqlite3"
    ).exists()
    assert (
        tmp_path
        / "train_test"
        / "skills"
        / "official"
        / "selected.json"
    ).exists()


def test_evaluate_runs_frozen_runtime_without_maintenance(tmp_path: Path):
    cfg = _config()
    runtime_model = MockClient(cfg.models["runtime"])
    _runtime_script(runtime_model)
    report = MiMEvaluator(
        cfg,
        RunDir.create("evaluate_test", tmp_path),
        runtime_model=runtime_model,
        embedder=Embedder("deterministic-hash"),
    ).evaluate(
        conversations=[_conversation()],
        questions={"conv_test": [Question(
            qa_id="conv_test_qa_0001",
            question="Where does Alice live?",
            reference_answer="Seattle",
            source_evidence=[["conv_test_s01", "conv_test:D1:1"]],
        )]},
        eval_ids=["conv_test"],
        mode="base",
        split_name="test",
    )
    assert report.total_qa == 1
    assert report.overall_f1 == 1.0
    assert report.protocol_errors == 0
    assert report.avg_construction_steps == 1.0


def test_failure_workflow_returns_independent_reports_and_search_steps(
    tmp_path: Path,
):
    cfg = _config()
    runtime_model = MockClient(cfg.models["runtime"])
    _runtime_script(runtime_model)
    runtime = MiMRuntime(
        cfg,
        mode="base",
        run_dir=RunDir.create("failure_source", tmp_path),
        runtime_model=runtime_model,
        embedder=Embedder("deterministic-hash"),
    )
    runtime.ingest(_conversation())

    question = Question(
        qa_id="conv_test_qa_0001",
        question="Where does Alice live?",
        reference_answer="Seattle",
        source_evidence=[["conv_test_s01", "conv_test:D1:1"]],
    )
    runtime.store.save_qa_case(
        qa_id=question.qa_id,
        conversation_id="conv_test",
        question=question.question,
        reference_answer=question.reference_answer,
        category=None,
        gold_message_ids=["conv_test:D1:1"],
    )
    runtime.store.save_access_trace(
        access_run_id="access_missing",
        run_id="failure_source",
        conversation_id="conv_test",
        qa_id=question.qa_id,
        snapshot_commit_id=runtime.latest_commit_id or 0,
        question=question.question,
        prediction="Boston",
        skill_version_ids=[],
        answer_prompt_hash="hash",
        action_records=[{
            "action_id": "access_missing_action_000",
            "step_index": 0,
            "action_type": "answer",
            "request": {"answer": "Boston"},
            "response": {},
            "hits": [],
        }],
        visible_memories=[],
        evidence_ids=[],
    )

    maintenance = MockClient(cfg.models["maintenance"])
    maintenance.set_script([
        maintenance._make_resp(json.dumps({
            "necessary_available_version_ids": [
                "mem_conv_test_0001_v1"
            ],
            "conflicting_returned_version_ids": [],
            "reason": "The residence memory existed but was not returned.",
            "confidence": 0.95,
            "review_required": False,
        })),
        maintenance._make_resp(json.dumps({
            "raw_support": "SUPPORTED",
            "construction_problem": False,
            "subtype": "none",
            "first_error": {},
            "reason": "The stored residence is correct.",
            "confidence": 0.95,
            "review_required": False,
        })),
        maintenance._make_resp("No information available."),
        maintenance._make_resp(json.dumps({
            "correct": False,
            "reason": "The answer is missing.",
        })),
    ])
    conn = runtime.store.open_read_connection()
    try:
        diagnoses = FailureWorkflow(
            access_agent=AccessDiagnosisAgent(maintenance),
            construction_agent=ConstructionDiagnosisAgent(maintenance),
            answer_check_agent=AnswerCheckAgent(maintenance),
            provenance=ProvenanceService(conn),
            output_dir=tmp_path / "failures",
        ).analyze(
            failure_id="failure_conv_test_qa_0001",
            run_id="failure_source",
            conversation_id="conv_test",
            qa_id=question.qa_id,
            snapshot_commit_id=runtime.latest_commit_id or 0,
            access_run_id="access_missing",
            question=question.question,
            prediction="Boston",
            reference_answer="Seattle",
            gold_message_ids=["conv_test:D1:1"],
            returned_memories=[],
            source_messages=runtime.store.get_source_messages(
                "conv_test", ["conv_test:D1:1"]
            ),
        )
    finally:
        conn.close()

    assert diagnoses.access.problem_found is True
    assert diagnoses.access.missing_necessary_version_ids == [
        "mem_conv_test_0001_v1"
    ]
    assert diagnoses.access.recommended_route == (
        LearningRoute.ACCESS_SKILL_MAKER
    )
    assert diagnoses.access.search_steps[0]["action_type"] == "answer"
    assert diagnoses.access.search_steps[0]["returned_memories"] == []
    assert diagnoses.construction.problem_found is False
    assert diagnoses.construction.recommended_route == LearningRoute.RECORD_ONLY
    assert diagnoses.answer_check["correct"] is False


def test_locomo_message_ids_are_globally_namespaced():
    data = [
        {
            "sample_id": sample_id,
            "conversation": {
                "speaker_a": "A",
                "session_1": [{"speaker": "A", "text": "x", "dia_id": "D1:1"}],
            },
            "qa": [{"question": "q", "answer": "x", "evidence": ["D1:1"]}],
        }
        for sample_id in ("conv-1", "conv-2")
    ]
    conversations, questions = _load_locomo_data(data)
    message_ids = [
        conversation.sessions[0].messages[0].message_id
        for conversation in conversations
    ]
    assert message_ids == ["conv-1:D1:1", "conv-2:D1:1"]
    assert questions["conv-2"][0].source_evidence == [["", "conv-2:D1:1"]]

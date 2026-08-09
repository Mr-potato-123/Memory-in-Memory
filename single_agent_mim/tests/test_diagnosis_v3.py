"""Offline tests for the isolated three-diagnosis architecture."""

from __future__ import annotations

import json
import runpy
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.agents.access_failure import AccessFailureAgent
from mim.agents.answer_failure import AnswerFailureAgent
from mim.agents.cons_failure import ConsFailureAgent
from mim.config import ModelConfig
from mim.diagnosis.artifacts import DiagnosisArtifactStore
from mim.diagnosis.evidence import DiagnosisEvidenceRepository
from mim.diagnosis.schemas import (
    DiagnosisCase,
    DiagnosisType,
)
from mim.diagnosis.workflows import ConsDiagnosisWorkflow
from mim.llm.mock_client import MockClient


def _case() -> DiagnosisCase:
    return DiagnosisCase(
        judge_run_id="judge_v2",
        diagnosis_run_id="diag_v3",
        source_runtime_run="runtime",
        conversation_id="conv",
        qa_id="qa",
        access_run_id="access",
        snapshot_commit_id=2,
        question="Where does Alice live?",
        reference_answer="Seattle",
        prediction="Boston",
        judge_label="I",
        judge_reason="The city is wrong.",
        gold_message_ids=["msg_1"],
    )


def _mock(*objects: dict) -> MockClient:
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    model.set_script(
        [model._make_resp(json.dumps(item)) for item in objects]
    )
    return model


def test_answer_failure_builds_access_package_when_context_is_sufficient():
    agent = AnswerFailureAgent(
        _mock({
            "essential_reference_claims": [{
                "claim": "Alice lives in Seattle.",
                "supporting_retrieved_version_ids": ["mem_v1"],
            }],
            "unresolved_material_contradiction": False,
            "reason": "The returned memory directly states the answer.",
            "confidence": 0.9,
            "review_required": False,
        }),
        prompt="answer prompt",
    )
    report = agent.diagnose(
        _case(),
        exact_search_steps=[{
            "step_index": 0,
            "returned_version_ids": ["mem_v1"],
            "returned_memories": [{
                "version_id": "mem_v1",
                "content": "Alice lives in Seattle.",
            }],
        }],
    )

    assert report.diagnosis_type == DiagnosisType.ANSWER_FAILURE
    assert report.problem_found is True
    assert report.retrieved_context_sufficient is True
    assert report.repair_package is not None
    assert report.repair_package["side"] == "access"
    assert report.repair_package["stage"] == "answer"
    assert report.repair_package["retrieved_context_sufficient"] is True
    assert report.repair_package["retrieved_version_ids"] == ["mem_v1"]


def test_answer_failure_handles_empty_unanswerable_reference():
    case = _case().model_copy(
        update={
            "reference_answer": "",
            "prediction": "An unsupported answer",
        }
    )
    agent = AnswerFailureAgent(
        _mock({
            "essential_reference_claims": [],
            "retrieved_context_supports_abstention": True,
            "unresolved_material_contradiction": False,
            "reason": "Nothing returned answers the question.",
            "confidence": 0.9,
            "review_required": False,
        }),
        prompt="answer prompt",
    )

    report = agent.diagnose(case, exact_search_steps=[])

    assert report.diagnosis_type == DiagnosisType.ANSWER_FAILURE
    assert report.problem_found is True
    assert report.retrieved_context_sufficient is True


def test_access_failure_uses_set_difference_over_current_memory():
    agent = AccessFailureAgent(
        _mock({
            "essential_reference_claims": [{
                "claim": "Alice lives in Seattle.",
                "supporting_current_version_ids": ["mem_v2"],
            }],
            "reason": "The useful current memory was not returned.",
            "confidence": 0.95,
            "review_required": False,
        }),
        prompt="access prompt",
    )
    report = agent.diagnose(
        _case(),
        current_related_memories=[{
            "version_id": "mem_v2",
            "memory_id": "mem",
            "content": "Alice lives in Seattle.",
        }],
        current_search_steps=[{
            "step_index": 0,
            "returned_version_ids": [],
            "returned_memories": [],
        }],
    )

    assert report.diagnosis_type == DiagnosisType.ACCESS_FAILURE
    assert report.missing_useful_current_version_ids == ["mem_v2"]
    assert report.repair_package is not None
    assert "query" not in report.repair_package


def test_empty_reference_skips_access_and_construction_gold_diagnosis():
    case = _case().model_copy(update={"reference_answer": ""})
    model = _mock()

    access = AccessFailureAgent(model, prompt="access prompt").diagnose(
        case,
        current_related_memories=[],
        current_search_steps=[],
    )
    screening = ConsFailureAgent(
        model,
        screening_prompt="screen prompt",
        trace_prompt="trace prompt",
    ).screen(case, current_related_memories=[])

    assert access.diagnosis_type == DiagnosisType.NO_ACCESS_FAILURE
    assert access.problem_found is False
    assert screening.cons_candidate is False


def test_access_evidence_view_removes_history_and_raw_sources():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE access_actions (
            action_id TEXT,
            access_run_id TEXT,
            step_index INTEGER,
            action_type TEXT,
            request_json TEXT,
            response_json TEXT
        );
        CREATE TABLE memory_versions (
            version_id TEXT,
            memory_id TEXT,
            version_no INTEGER,
            conversation_id TEXT,
            content TEXT,
            memory_kind TEXT,
            subject TEXT,
            predicate TEXT,
            object_text TEXT,
            world_start TEXT,
            world_end TEXT,
            update_type TEXT,
            system_from_commit INTEGER,
            system_to_commit INTEGER
        );
        CREATE TABLE memory_lineage_messages (
            version_id TEXT,
            message_id TEXT
        );
        """
    )
    conn.executemany(
        """INSERT INTO memory_versions VALUES
           (?, 'mem', ?, 'conv', ?, 'state', 'Alice', 'residence',
            'Seattle', NULL, NULL, 'update', ?, ?)""",
        [
            ("mem_v1", 1, "Alice lived in Boston.", 1, 2),
            ("mem_v2", 2, "Alice lives in Seattle.", 2, None),
        ],
    )
    conn.execute(
        "INSERT INTO memory_lineage_messages VALUES ('mem_v2', 'msg_1')"
    )
    conn.execute(
        """INSERT INTO access_actions VALUES
           ('a1', 'access', 0, 'inspect_memory', '{}', ?)""",
        (json.dumps({
            "versions": [
                {"version_id": "mem_v1", "content": "old"},
                {"version_id": "mem_v2", "content": "current"},
            ],
            "sources": [{"message_id": "msg_1", "content": "raw secret"}],
        }),),
    )

    repository = DiagnosisEvidenceRepository(conn)
    exact = repository.exact_runtime_search_chain("access")
    current = repository.current_access_search_chain(
        access_run_id="access",
        conversation_id="conv",
        snapshot_commit_id=2,
    )

    assert exact[0]["returned_version_ids"] == ["mem_v1", "mem_v2"]
    assert current[0]["returned_version_ids"] == ["mem_v2"]
    assert "response" not in current[0]
    assert "raw secret" not in json.dumps(current)
    assert repository.current_related_memories(
        conversation_id="conv",
        message_ids=["msg_1"],
        snapshot_commit_id=2,
    )[0]["version_id"] == "mem_v2"
    conn.close()


class _TrackingEvidence:
    def __init__(self):
        self.source_calls = 0
        self.history_calls = 0

    def current_related_memories(self, **_kwargs):
        return [{"version_id": "mem_v2", "content": "Alice lives in Seattle."}]

    def source_messages(self, **_kwargs):
        self.source_calls += 1
        return [{"message_id": "msg_1", "content": "I live in Seattle."}]

    def construction_history(self, **_kwargs):
        self.history_calls += 1
        return {
            "processed_commits": [{"message_id": "msg_1", "commit_id": 1}],
            "candidates": [],
            "change_events": [],
            "snapshot_memories": [{
                "version_id": "mem_v2",
                "memory_id": "mem",
            }],
        }


def test_cons_does_not_load_raw_or_history_when_stage_a_is_full():
    evidence = _TrackingEvidence()
    agent = ConsFailureAgent(
        _mock({
            "essential_reference_claims": [{
                "claim": "Alice lives in Seattle.",
                "supporting_current_version_ids": ["mem_v2"],
                "coverage": "FULL",
            }],
            "reason": "Current memory is complete.",
            "confidence": 0.9,
            "review_required": False,
        }),
        screening_prompt="screen",
        trace_prompt="trace",
    )
    report = ConsDiagnosisWorkflow(
        agent=agent,
        evidence=evidence,
    ).run(_case())

    assert report.diagnosis_type == DiagnosisType.NO_CONS_FAILURE
    assert evidence.source_calls == 0
    assert evidence.history_calls == 0


def test_cons_candidate_loads_history_and_reports_first_error():
    evidence = _TrackingEvidence()
    agent = ConsFailureAgent(
        _mock(
            {
                "essential_reference_claims": [{
                    "claim": "Alice lives in Seattle.",
                    "supporting_current_version_ids": [],
                    "coverage": "MISSING",
                }],
                "reason": "The current entry does not preserve the claim.",
                "confidence": 0.9,
                "review_required": False,
            },
            {
                "raw_support": "SUPPORTED",
                "construction_problem": True,
                "affected_reference_claim": "Alice lives in Seattle.",
                "affected_memory_ids": [],
                "subtype": "extraction",
                "first_error": {
                    "stage": "extraction",
                    "message_ids": ["msg_1"],
                    "candidate_id": None,
                    "decision_id": None,
                    "commit_id": 1,
                    "change_id": None,
                    "operation": None,
                    "before_version_ids": [],
                    "after_version_id": None,
                },
                "reason": "The raw statement was present but no candidate kept it.",
                "confidence": 0.9,
                "review_required": False,
            },
        ),
        screening_prompt="screen",
        trace_prompt="trace",
    )
    report = ConsDiagnosisWorkflow(
        agent=agent,
        evidence=evidence,
    ).run(_case())

    assert report.diagnosis_type == DiagnosisType.CONS_FAILURE
    assert report.first_error["stage"] == "extraction"
    assert evidence.source_calls == 1
    assert evidence.history_calls == 1
    assert report.repair_package is not None


def test_answer_artifact_keeps_audit_log_and_creates_access_package(
    tmp_path: Path,
):
    report = AnswerFailureAgent(
        _mock({
            "essential_reference_claims": [{
                "claim": "Alice lives in Seattle.",
                "supporting_retrieved_version_ids": ["mem_v1"],
            }],
            "unresolved_material_contradiction": False,
            "reason": "Enough context.",
            "confidence": 0.9,
            "review_required": False,
        }),
        prompt="answer",
    ).diagnose(
        _case(),
        exact_search_steps=[{
            "returned_version_ids": ["mem_v1"],
            "returned_memories": [{
                "version_id": "mem_v1",
                "content": "Alice lives in Seattle.",
            }],
        }],
    )
    store = DiagnosisArtifactStore(
        tmp_path,
        component="answer",
        resume=False,
    )
    store.publish(report)

    assert store.answer_failures_path.exists()
    package_path = store.packages_root / "conv" / "qa_answer_failure.json"
    assert package_path.exists()
    package = json.loads(package_path.read_text(encoding="utf-8"))
    assert package["repair_package"]["stage"] == "answer"


def test_candidate_collector_routes_answer_and_access_to_access_side(
    tmp_path: Path,
):
    for component, problem_found in (
        ("answer_failure", True),
        ("access_failure", True),
        ("cons_failure", True),
        ("answer_failure_ignored", False),
    ):
        actual_component = component.removesuffix("_ignored")
        package_dir = tmp_path / actual_component / "packages" / component
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "report.json").write_text(
            json.dumps({
                "diagnosis_id": component,
                "problem_found": problem_found,
                "repair_package": {"stage": "answer"},
            }),
            encoding="utf-8",
        )

    script = Path(__file__).resolve().parents[1] / "scripts" / (
        "run_candidates_from_diagnosis.py"
    )
    collect = runpy.run_path(str(script))["_collect_packages"]
    rows = collect(tmp_path)

    assert [(row["report"]["diagnosis_id"], row["side"]) for row in rows] == [
        ("answer_failure", "access"),
        ("access_failure", "access"),
        ("cons_failure", "construction"),
    ]

"""Agent protocol and isolated semantic-call tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mim.agents.access import AccessAgent
from mim.agents.access_diagnosis import AccessDiagnosisAgent
from mim.agents.construction import ConstructionAgent
from mim.agents.construction_diagnosis import ConstructionDiagnosisAgent
from mim.agents.failure import AnswerCheckAgent
from mim.agents.skill_learning import CandidateSkillAgent
from mim.config import ModelConfig
from mim.failure.schemas import DiagnosisStatus, LearningRoute
from mim.llm.mock_client import MockClient


def _mock() -> MockClient:
    return MockClient(ModelConfig(provider="mock", model="mock"))


class _CapturingMock(MockClient):
    def __init__(self):
        super().__init__(ModelConfig(provider="mock", model="mock"))
        self.seen_messages = []

    def generate(self, messages, **kwargs):
        self.seen_messages.append(messages)
        return super().generate(messages, **kwargs)


def test_access_action_parser_accepts_exact_protocol():
    action = AccessAgent._parse_action(json.dumps({
        "action": "search_memory",
        "arguments": {
            "query": "prior residence",
            "strategy": "hybrid",
            "include_history": True,
        },
        "reason": "Historical state is required.",
    }))
    assert action is not None
    assert action.action == "search_memory"
    assert action.arguments["include_history"] is True


def test_access_action_parser_rejects_non_json():
    assert AccessAgent._parse_action("not an action") is None


def test_construction_normalizes_version_target_to_logical_memory():
    related = [SimpleNamespace(version_id="mem_1_v2", memory_id="mem_1")]
    assert ConstructionAgent._logical_memory_id("mem_1_v2", related) == "mem_1"


def test_failure_agent_keeps_blind_reanswer_reference_isolated():
    model = _CapturingMock()
    model.set_script([model._make_resp("Seattle")])
    agent = AnswerCheckAgent(model)
    answer = agent.reanswer(
        question="Where does the person live?",
        returned_memories=[
            {"version_id": "mem_v1", "content": "The person lives in Seattle."}
        ],
    )
    assert answer == "Seattle"
    sent = model.seen_messages[0]
    assert all("reference" not in message["content"].lower() for message in sent)


def test_failure_agent_includes_world_time_in_memory_input():
    model = _CapturingMock()
    model.set_script([model._make_resp("21 July 2023")])
    agent = AnswerCheckAgent(model)
    agent.reanswer(
        question="When did they collaborate?",
        returned_memories=[{
            "version_id": "mem_v1",
            "memory_kind": "event",
            "subject": "Jon and Gina",
            "content": "They decided to collaborate.",
            "world_start": "2023-07-21",
            "world_end": None,
        }],
    )
    prompt = model.seen_messages[0][0]["content"]
    assert "world_start=2023-07-21" in prompt
    assert "subject=Jon and Gina" in prompt


def test_access_conflict_without_missing_memory_is_not_repair_route():
    model = _mock()
    model.set_script([model._make_resp(json.dumps({
        "necessary_available_version_ids": ["mem_v1"],
        "conflicting_returned_version_ids": ["mem_v2"],
        "reason": "The necessary memory and a contradiction were returned.",
        "confidence": 0.9,
        "review_required": False,
    }))])
    result = AccessDiagnosisAgent(model).diagnose(
        failure_id="failure",
        run_id="run",
        conversation_id="conv",
        qa_id="qa",
        access_run_id="access",
        snapshot_commit_id=1,
        question="Where?",
        prediction="Boston",
        reference_answer="Seattle",
        relevant_snapshot_memories=[
            {"version_id": "mem_v1"},
            {"version_id": "mem_v2"},
        ],
        search_steps=[{
            "step_index": 0,
            "returned_version_ids": ["mem_v1", "mem_v2"],
        }],
    )
    assert result.problem_found is False
    assert result.primary_subtype == "no_retrieval_problem"
    assert result.recommended_route == LearningRoute.RECORD_ONLY
    assert result.conflicting_returned_version_ids == ["mem_v2"]


def test_construction_extraction_distortion_is_learnable():
    model = _mock()
    model.set_script([model._make_resp(json.dumps({
        "raw_support": "SUPPORTED",
        "construction_problem": True,
        "subtype": "extraction_distortion",
        "first_error": {
            "stage": "extraction_distortion",
            "message_ids": ["msg_1"],
            "candidate_id": "candidate_1",
            "decision_id": "decision_1",
            "commit_id": 2,
            "operation": "ADD",
            "before_version_ids": [],
            "after_version_id": None,
        },
        "reason": "The candidate changed an answer-bearing detail.",
        "confidence": 0.95,
        "review_required": False,
    }))])
    result = ConstructionDiagnosisAgent(model).diagnose(
        failure_id="failure",
        run_id="run",
        conversation_id="conv",
        qa_id="qa",
        snapshot_commit_id=2,
        question="What changed?",
        prediction="wrong",
        reference_answer="right",
        raw_message_ids=["msg_1"],
        source_messages=[{"message_id": "msg_1", "content": "right"}],
        construction_history={
            "processed_commits": [{"message_id": "msg_1", "commit_id": 1}],
            "candidates": [{
                "candidate_id": "candidate_1",
                "decision_id": "decision_1",
                "commit_id": 2,
            }],
            "change_events": [{
                "decision_id": "decision_1",
                "commit_id": 2,
                "before_versions": [{"version_id": "mem_v1"}],
                "after_version": {"version_id": "mem_v2"},
            }],
            "snapshot_memories": [{"version_id": "mem_v2"}],
        },
    )
    assert result.first_error["stage"] == "extraction_distortion"
    assert result.first_broken_edge == "message_to_candidate"
    assert result.review_required is False
    assert result.recommended_route == LearningRoute.CONSTRUCTION_SKILL_MAKER
    assert result.repair_package


def test_construction_persistence_failure_is_engineering_issue():
    model = _mock()
    model.set_script([model._make_resp(json.dumps({
        "raw_support": "SUPPORTED",
        "construction_problem": True,
        "subtype": "persistence",
        "first_error": {
            "stage": "persistence",
            "message_ids": ["msg_1"],
            "candidate_id": "candidate_1",
            "decision_id": "decision_1",
            "commit_id": 2,
            "operation": "ADD",
            "before_version_ids": [],
            "after_version_id": None,
        },
        "reason": "A faithful candidate was not persisted.",
        "confidence": 0.95,
        "review_required": False,
    }))])
    result = ConstructionDiagnosisAgent(model).diagnose(
        failure_id="failure", run_id="run", conversation_id="conv",
        qa_id="qa", snapshot_commit_id=2, question="Where?",
        prediction="wrong", reference_answer="right",
        raw_message_ids=["msg_1"],
        source_messages=[{"message_id": "msg_1", "content": "right"}],
        construction_history={
            "processed_commits": [{"message_id": "msg_1", "commit_id": 1}],
            "candidates": [{"candidate_id": "candidate_1",
                            "decision_id": "decision_1", "commit_id": 2}],
            "change_events": [], "snapshot_memories": [],
        },
    )
    assert result.recommended_route == LearningRoute.ENGINEERING_ISSUE
    assert result.repair_package == {}


def test_failure_semantic_json_fallback_is_conservative():
    model = _mock()
    model.set_script([model._make_resp("broken output")])
    result = AccessDiagnosisAgent(model).diagnose(
        failure_id="failure",
        run_id="run",
        conversation_id="conv",
        qa_id="qa",
        access_run_id="access",
        snapshot_commit_id=1,
        question="Where?",
        prediction="Boston",
        reference_answer="Seattle",
        relevant_snapshot_memories=[],
        search_steps=[],
    )
    assert result.status == DiagnosisStatus.MODEL_ERROR


def test_candidate_skill_repairs_length_without_broadening():
    model = _CapturingMock()
    too_long = "x" * 601
    model.set_script([
        model._make_resp(json.dumps({
            "decision": "PROPOSE_SKILL",
            "solves": too_long,
            "related_existing_skill_ids": [],
            "skill": {
                "name": "Narrow retry",
                "description": "Use when an exact trigger is observed.",
                "content": ["Perform one bounded retry, then stop."],
            },
        })),
        model._make_resp(json.dumps({
            "decision": "PROPOSE_SKILL",
            "solves": (
                "Fixes one missed exact-trigger retry; applies only when the "
                "first exact search is empty, not when evidence is sufficient."
            ),
            "related_existing_skill_ids": [],
            "skill": {
                "name": "Narrow retry",
                "description": "Use when an exact trigger is observed.",
                "content": ["Perform one bounded retry, then stop."],
            },
        })),
    ])
    candidate = CandidateSkillAgent(model, prompt="Return JSON.").generate(
        diagnosis={"diagnosis_id": "diag_1"},
        side="access",
    )

    assert candidate is not None
    assert len(candidate.solves) < 600
    assert len(model.seen_messages) == 2
    assert "Compress wording instead of broadening scope" in (
        model.seen_messages[1][-1]["content"]
    )


def test_w2w_candidate_keeps_maintenance_lineage_outside_skill_payload():
    model = _CapturingMock()
    model.set_script([model._make_resp(json.dumps({
        "decision": "PROPOSE_SKILL",
        "maintenance_intent": "REVISE",
        "why_previous_round_failed": "The selected rule did not enforce coverage.",
        "solves": "Repairs a repeated evidence-coverage failure.",
        "related_existing_skill_ids": ["sk_access_existing"],
        "skill": {
            "name": "Verify unresolved evidence gap",
            "description": "Use when the first search lacks one required claim; not when all claims are directly supported.",
            "content": [
                "Search once for the missing claim; do not apply when the first result already supports the complete answer."
            ],
        },
    }))])

    candidate = CandidateSkillAgent(model, prompt="Return JSON.").generate(
        diagnosis={
            "diagnosis_id": "persistent_case_access",
            "transition": "W2W",
            "failure_age": 3,
            "maintenance_intent_hint": "REVISE",
            "failure_to_repair": {
                "why_previous_round_failed": "The selected rule did not enforce coverage."
            },
        },
        side="access",
    )

    assert candidate is not None
    assert candidate.transition == "W2W"
    assert candidate.failure_age == 3
    assert candidate.maintenance_intent == "REVISE"
    assert candidate.why_previous_round_failed
    assert "previous" not in candidate.payload.description.lower()

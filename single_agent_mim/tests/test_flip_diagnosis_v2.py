"""Offline contract tests for claim-shared contrastive diagnosis v2."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.agents.flip_failure import (
    FlipDiagnosisAgent,
    PersistentFailureDiagnosisAgent,
)
from mim.config import ModelConfig
from mim.llm.mock_client import MockClient


def _agent(result: dict) -> FlipDiagnosisAgent:
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    model.set_script([model._make_resp(json.dumps(result))])
    return FlipDiagnosisAgent(model, prompt="contrastive prompt")


def _payload() -> dict:
    return {
        "qa_id": "conv-1_qa-1",
        "conversation_id": "conv-1",
        "question": "What happened?",
        "reference_answer": "A and B",
        "flip": {"chain": "empty_to_b1", "direction": "W2C"},
        "correct_side": {
            "answer": "A and B",
            "final_evidence_ids": ["ok_v1"],
            "skill_trace": {"selected": [{"skill_id": "access_ok"}]},
            "construction_skill_traces": [
                {"selected": [{"skill_id": "cons_ok"}]}
            ],
            "construction_traces": [{"session_id": "s1"}],
            "current_memories": [{"version_id": "ok_v1"}],
        },
        "wrong_side": {
            "answer": "A",
            "final_evidence_ids": ["bad_v1"],
            "skill_trace": {"selected": [{"skill_id": "access_bad"}]},
            "construction_skill_traces": [
                {"selected": [{"skill_id": "cons_bad"}]}
            ],
            "construction_traces": [{"session_id": "s1"}],
            "current_memories": [{"version_id": "bad_v1"}],
        },
    }


def _claim(deltas: dict) -> dict:
    return {
        "claim_id": "claim_01",
        "claim": "B happened",
        "correct_side": {
            "memory_coverage": "FULL",
            "supporting_current_version_ids": ["ok_v1"],
            "retrieval_coverage": "FULL",
            "retrieved_supporting_version_ids": ["ok_v1"],
            "answer_coverage": "CORRECT",
        },
        "wrong_side": {
            "memory_coverage": "PARTIAL",
            "supporting_current_version_ids": ["bad_v1"],
            "retrieval_coverage": "NONE",
            "retrieved_supporting_version_ids": [],
            "answer_coverage": "MISSING",
        },
        "deltas": deltas,
    }


def test_fixed_search_delta_is_not_projected_with_construction():
    result = {
        "claims": [_claim({"construction": True, "access": True, "answer": False})],
        "attribution": {
            "construction": True,
            "access": True,
            "answer": False,
            "learnable": True,
            "confidence": 0.9,
            "reason": "Memory loss and retrieval omission both matter.",
        },
        "mechanisms": {"access": {}, "construction": {}},
    }
    report = _agent(result).diagnose(_payload())

    assert report["core"]["schema_version"] == "contrastive_core_v2"
    assert [item["stage"] for item in report["projections"]] == ["construction"]
    cons = report["projections"][0]
    assert cons["construction_skill_traces"][0]["selected"][0]["skill_id"] == "cons_bad"
    assert "access_bad" not in json.dumps(cons["construction_skill_traces"])


def test_fixed_search_failure_is_not_misreported_as_answer_skill():
    result = {
        "claims": [_claim({"construction": False, "access": True, "answer": True})],
        "attribution": {
            "construction": False,
            "access": True,
            "answer": True,
            "learnable": True,
            "reason": "Model incorrectly requested two stages.",
        },
    }
    report = _agent(result).diagnose(_payload())

    assert report["core"]["attribution"]["answer"] is False
    assert report["core"]["attribution"]["learnable"] is False
    assert report["projections"] == []


def test_pure_answer_projects_to_access_generator():
    claim = _claim({"construction": False, "access": False, "answer": True})
    claim["wrong_side"]["memory_coverage"] = "FULL"
    claim["wrong_side"]["retrieval_coverage"] = "FULL"
    result = {
        "claims": [claim],
        "attribution": {
            "construction": False,
            "access": False,
            "answer": True,
            "learnable": True,
            "reason": "Same evidence, wrong composition.",
        },
        "mechanisms": {"answer": {"subtype": "CLAIM_COMPOSITION"}},
    }
    projection = _agent(result).diagnose(_payload())["projections"][0]

    assert projection["stage"] == "answer"
    assert projection["side"] == "access"
    assert projection["diagnosis_type"] == "ANSWER_FAILURE"
    assert projection["retrieved_context_sufficient"] is True
    assert projection["skill_learnable"] is True
    assert projection["repair_package"]["eligible_for_skill_generation"] is True
    assert projection["repair_package"]["claim_evidence_parity"] is True


def test_empty_reference_abstention_projects_to_access_generator():
    payload = _payload()
    payload["reference_answer"] = ""
    payload["correct_side"]["answer"] = ""
    payload["wrong_side"]["answer"] = "Unsupported guess"
    result = {
        "claims": [],
        "attribution": {
            "construction": False,
            "access": False,
            "answer": True,
            "learnable": True,
            "reason": "The wrong side failed to abstain.",
        },
        "mechanisms": {"answer": {"subtype": "ABSTENTION"}},
    }

    report = _agent(result).diagnose(payload)

    assert report["core"]["attribution"]["answer"] is True
    assert [item["stage"] for item in report["projections"]] == ["answer"]
    assert report["projections"][0]["side"] == "access"


def test_w2w_fixed_search_failure_is_record_only():
    model = MockClient(ModelConfig(provider="mock", model="mock"))
    result = {
        "claims": [{
            "claim_id": "claim_01",
            "claim": "B happened",
            "prior_side": {
                "memory_coverage": "FULL",
                "supporting_current_version_ids": ["prior_v1"],
                "retrieval_coverage": "NONE",
                "retrieved_supporting_version_ids": [],
                "answer_coverage": "MISSING",
            },
            "current_side": {
                "memory_coverage": "FULL",
                "supporting_current_version_ids": ["current_v1"],
                "retrieval_coverage": "NONE",
                "retrieved_supporting_version_ids": [],
                "answer_coverage": "MISSING",
            },
            "failure": {
                "construction": False,
                "access": True,
                "answer": False,
                "persisted": True,
            },
        }],
        "attribution": {
            "construction": False,
            "access": True,
            "answer": False,
            "learnable": True,
            "confidence": 0.9,
            "reason": "The useful memory remained unretrieved.",
        },
        "failure_to_repair": {
            "type": "SKILL_NOT_RETRIEVED",
            "why_previous_round_failed": "The prior rule never routed.",
        },
        "mechanisms": {"access": {}},
    }
    model.set_script([model._make_resp(json.dumps(result))])
    payload = {
        "qa_id": "conv-1_qa-1",
        "conversation_id": "conv-1",
        "question": "What happened?",
        "reference_answer": "B",
        "transition": {"chain": "b0_to_b1", "direction": "W2W"},
        "failure_age": 2,
        "gold_answer_path": {"source_messages": [{"content": "B happened"}]},
        "repair_lineage": {"current_selected_skill_ids": []},
        "prior_side": {
            "answer": "A",
            "current_memories": [{"version_id": "prior_v1"}],
            "visible_memories": [],
            "final_evidence_ids": [],
            "skill_trace": {"selected": []},
        },
        "current_side": {
            "answer": "A",
            "current_memories": [{"version_id": "current_v1"}],
            "visible_memories": [],
            "final_evidence_ids": [],
            "skill_trace": {"selected": []},
        },
    }

    report = PersistentFailureDiagnosisAgent(
        model, prompt="persistent prompt"
    ).diagnose(payload)

    assert report["core"]["schema_version"] == "persistent_failure_core_v1"
    assert report["problem_found"] is False
    assert report["projections"] == []

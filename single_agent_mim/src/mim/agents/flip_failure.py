"""Contrastive diagnosis for correct/wrong runs of the same question.

The model builds one shared claim-level core.  Python then enforces the
attribution rules and projects that core into the small packages consumed by
the two isolated candidate generators.
"""

from __future__ import annotations

from typing import Any

from ..diagnosis.model_io import call_json, confidence, unique_strings
from ..llm.base import ModelClient


_COVERAGE = {"FULL", "PARTIAL", "MISSING", "INCORRECT", "NONE"}


def _coverage(value: Any, *, default: str = "NONE") -> str:
    normalized = str(value or default).upper()
    return normalized if normalized in _COVERAGE else default


def _side_state(value: Any, *, allowed: dict[str, set[str]]) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    return {
        "memory_coverage": _coverage(value.get("memory_coverage")),
        "supporting_current_version_ids": [
            item for item in unique_strings(value.get("supporting_current_version_ids"))
            if item in allowed["current"]
        ],
        "retrieval_coverage": _coverage(value.get("retrieval_coverage")),
        "retrieved_supporting_version_ids": [
            item for item in unique_strings(value.get("retrieved_supporting_version_ids"))
            if item in allowed["visible"]
        ],
        "cited_version_ids": [
            item for item in unique_strings(value.get("cited_version_ids"))
            if item in allowed["final"]
        ],
        "answer_coverage": _coverage(value.get("answer_coverage")),
    }


def _normalize_claims(value: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return claims
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict) or not str(raw.get("claim", "")).strip():
            continue
        allowed_by_side: dict[str, dict[str, set[str]]] = {}
        for side in ("correct_side", "wrong_side"):
            supplied = payload.get(side, {})
            allowed_by_side[side] = {
                "current": {
                    str(item.get("version_id"))
                    for item in supplied.get("current_memories", [])
                    if isinstance(item, dict) and item.get("version_id")
                },
                "visible": {
                    str(item.get("version_id"))
                    for item in supplied.get("visible_memories", [])
                    if isinstance(item, dict) and item.get("version_id")
                },
                "final": {
                    str(item) for item in supplied.get("final_evidence_ids", []) if item
                },
            }
        correct = _side_state(
            raw.get("correct_side"), allowed=allowed_by_side["correct_side"]
        )
        wrong = _side_state(
            raw.get("wrong_side"), allowed=allowed_by_side["wrong_side"]
        )
        deltas = raw.get("deltas") if isinstance(raw.get("deltas"), dict) else {}
        claims.append({
            "claim_id": str(raw.get("claim_id") or f"claim_{index:02d}"),
            "claim": str(raw["claim"]).strip(),
            "correct_side": correct,
            "wrong_side": wrong,
            "deltas": {
                "construction": bool(deltas.get("construction", False)),
                "access": bool(deltas.get("access", False)),
                "answer": bool(deltas.get("answer", False)),
            },
        })
    return claims


def _normalize_persistent_claims(
    value: Any, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Normalize claim coverage for two wrong runs without inventing a good run."""
    claims: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return claims
    allowed_by_side: dict[str, dict[str, set[str]]] = {}
    for side in ("prior_side", "current_side"):
        supplied = payload.get(side, {})
        allowed_by_side[side] = {
            "current": {
                str(item.get("version_id"))
                for item in supplied.get("current_memories", [])
                if isinstance(item, dict) and item.get("version_id")
            },
            "visible": {
                str(item.get("version_id"))
                for item in supplied.get("visible_memories", [])
                if isinstance(item, dict) and item.get("version_id")
            },
            "final": {
                str(item) for item in supplied.get("final_evidence_ids", []) if item
            },
        }
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict) or not str(raw.get("claim", "")).strip():
            continue
        prior = _side_state(raw.get("prior_side"), allowed=allowed_by_side["prior_side"])
        current = _side_state(
            raw.get("current_side"), allowed=allowed_by_side["current_side"]
        )
        failure = raw.get("failure") if isinstance(raw.get("failure"), dict) else {}
        claims.append({
            "claim_id": str(raw.get("claim_id") or f"claim_{index:02d}"),
            "claim": str(raw["claim"]).strip(),
            "prior_side": prior,
            "current_side": current,
            "failure": {
                "construction": bool(failure.get("construction", False)),
                "access": bool(failure.get("access", False)),
                "answer": bool(failure.get("answer", False)),
                "persisted": bool(failure.get("persisted", True)),
            },
        })
    return claims


class FlipDiagnosisAgent:
    """Build one contrastive core and deterministic side projections."""

    def __init__(self, model: ModelClient, *, prompt: str):
        self._model = model
        self._prompt = prompt

    def diagnose(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = call_json(
            self._model, prompt=self._prompt, payload=payload, max_tokens=8000
        )
        claims = _normalize_claims(result.get("claims"), payload)
        raw_attr = result.get("attribution")
        raw_attr = raw_attr if isinstance(raw_attr, dict) else {}

        construction = bool(raw_attr.get("construction")) or any(
            claim["deltas"]["construction"] for claim in claims
        )
        access = bool(raw_attr.get("access")) or any(
            claim["deltas"]["access"] for claim in claims
        )
        answer_requested = bool(raw_attr.get("answer")) or any(
            claim["deltas"]["answer"] for claim in claims
        )
        # Answer is a residual behavior error only when both sides had the
        # necessary memory and retrieval.  It can never coexist with an
        # upstream access/construction attribution.
        # An empty reference is the explicit "unanswerable / abstain" case.
        # It has no positive claim whose memory/retrieval coverage can be FULL,
        # yet a correct abstention versus an unsupported answer is still a
        # reusable Answer-stage contrast.  Treat the empty claim set as
        # evidence parity only when the model found no upstream difference.
        reference_is_empty = not str(payload.get("reference_answer", "")).strip()
        answer_evidence_parity = (
            reference_is_empty
            or (
                bool(claims)
                and all(
                    claim[side][field] == "FULL"
                    for claim in claims
                    for side in ("correct_side", "wrong_side")
                    for field in ("memory_coverage", "retrieval_coverage")
                )
            )
        )
        answer = (
            answer_requested and answer_evidence_parity
            and not access and not construction
        )
        learnable = bool(raw_attr.get("learnable", True)) and bool(
            answer or access or construction
        )
        if not learnable:
            answer = access = construction = False

        core = {
            "schema_version": "contrastive_core_v2",
            "pair_id": (
                f"flip_{payload.get('flip', {}).get('chain', 'chain')}_"
                f"{payload['qa_id']}"
            ),
            "qa_id": payload["qa_id"],
            "conversation_id": payload.get("conversation_id", ""),
            "question": payload.get("question", ""),
            "reference_answer": payload.get("reference_answer", ""),
            "comparison": payload.get("flip", {}),
            "claims": claims,
            "attribution": {
                "answer": answer,
                "access": access,
                "construction": construction,
                "learnable": learnable,
                "confidence": confidence(raw_attr.get("confidence")),
                "reason": str(raw_attr.get("reason") or result.get("reason", "")).strip(),
            },
            "mechanisms": (
                result.get("mechanisms")
                if isinstance(result.get("mechanisms"), dict)
                else {}
            ),
        }
        return {
            "schema_version": "contrastive_diagnosis_v2",
            "problem_found": learnable,
            "reason": core["attribution"]["reason"],
            "confidence": core["attribution"]["confidence"],
            "review_required": bool(result.get("review_required", False)),
            "core": core,
            "projections": self._project(core, payload) if learnable else [],
        }

    @staticmethod
    def _project(core: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        projections: list[dict[str, Any]] = []
        direction = str(core["comparison"].get("direction", ""))
        polarity = "ADOPT" if direction == "W2C" else "PRESERVE_AVOID"
        mechanisms = core.get("mechanisms", {})
        correct = payload.get("correct_side", {})
        wrong = payload.get("wrong_side", {})

        def base(stage: str, side: str) -> dict[str, Any]:
            diagnosis_id = f"{core['pair_id']}_{stage}"
            return {
                "schema_version": f"contrastive_{stage}_v2",
                "diagnosis_id": diagnosis_id,
                "diagnosis_type": (
                    "CONS_FAILURE" if side == "construction" else "ACCESS_FAILURE"
                ),
                "status": "completed",
                "problem_found": True,
                "source_mode": "contrastive",
                "side": side,
                "stage": stage,
                "qa_id": core["qa_id"],
                "conversation_id": core["conversation_id"],
                "question": core["question"],
                "reference_answer": core["reference_answer"],
                "prediction": str(wrong.get("answer", "")),
                "flip": core["comparison"],
                "learning_polarity": polarity,
                "reason": core["attribution"]["reason"],
                "confidence": core["attribution"]["confidence"],
                "review_required": False,
            }

        if core["attribution"]["answer"]:
            report = base("answer", "access")
            relevant = [claim for claim in core["claims"] if claim["deltas"]["answer"]]
            report["skill_trace"] = wrong.get("skill_trace")
            report["repair_package"] = {
                "stage": "answer",
                "claim_evidence_parity": True,
                "claim_deltas": relevant or core["claims"],
                "correct_behavior": {
                    "answer": correct.get("answer", ""),
                    "final_evidence_ids": correct.get("final_evidence_ids", []),
                },
                "wrong_behavior": {
                    "answer": wrong.get("answer", ""),
                    "final_evidence_ids": wrong.get("final_evidence_ids", []),
                },
                "mechanism": mechanisms.get("answer", {}),
            }
            projections.append(report)

        if core["attribution"]["access"]:
            report = base("access", "access")
            relevant = [claim for claim in core["claims"] if claim["deltas"]["access"]]
            report["skill_trace"] = wrong.get("skill_trace")
            report["repair_package"] = {
                "stage": "retrieval",
                "claim_deltas": relevant,
                "correct_behavior": {
                    "skill_trace": correct.get("skill_trace"),
                    "search_actions": correct.get("search_actions", []),
                    "visible_memories": correct.get("visible_memories", []),
                },
                "wrong_behavior": {
                    "skill_trace": wrong.get("skill_trace"),
                    "search_actions": wrong.get("search_actions", []),
                    "visible_memories": wrong.get("visible_memories", []),
                },
                "mechanism": mechanisms.get("access", {}),
            }
            projections.append(report)

        if core["attribution"]["construction"]:
            report = base("construction", "construction")
            relevant = [
                claim for claim in core["claims"] if claim["deltas"]["construction"]
            ]
            correct_traces = correct.get("construction_skill_traces", [])
            wrong_traces = wrong.get("construction_skill_traces", [])
            # These are genuine Construction traces, never Access skill IDs.
            report["construction_skill_traces"] = wrong_traces
            construction_mechanism = mechanisms.get("construction", {})
            subtype = str(construction_mechanism.get("subtype", "")).lower()
            learnable_subtypes = {
                "extraction",
                "extraction_omission",
                "extraction_distortion",
                "temporal_metadata",
                "relation_judgment",
                "wrong_skip",
            }
            if subtype and subtype not in learnable_subtypes:
                return projections
            report["repair_package"] = {
                "schema_version": "fixed_c1_c2_contrast_v1",
                "stage": "construction",
                "learnable_stage": subtype or "extraction",
                "claim_deltas": relevant,
                "source_messages": wrong.get("source_messages", []),
                "correct_behavior": {
                    "construction_skill_traces": correct_traces,
                    "construction_traces": correct.get("construction_traces", []),
                    "current_memories": correct.get("current_memories", []),
                },
                "wrong_behavior": {
                    "construction_skill_traces": wrong_traces,
                    "construction_traces": wrong.get("construction_traces", []),
                    "current_memories": wrong.get("current_memories", []),
                },
                "earliest_divergence": mechanisms.get("construction", {}).get(
                    "earliest_divergence"
                ),
                "mechanism": construction_mechanism,
            }
            projections.append(report)
        return projections


class PersistentFailureDiagnosisAgent:
    """Diagnose W2W cases using gold evidence plus prior/current wrong runs."""

    def __init__(self, model: ModelClient, *, prompt: str):
        self._model = model
        self._prompt = prompt

    def diagnose(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = call_json(
            self._model, prompt=self._prompt, payload=payload, max_tokens=8000
        )
        claims = _normalize_persistent_claims(result.get("claims"), payload)
        raw_attr = result.get("attribution")
        raw_attr = raw_attr if isinstance(raw_attr, dict) else {}

        construction = bool(raw_attr.get("construction")) or any(
            claim["failure"]["construction"] for claim in claims
        )
        access = bool(raw_attr.get("access")) or any(
            claim["failure"]["access"] for claim in claims
        )
        answer_requested = bool(raw_attr.get("answer")) or any(
            claim["failure"]["answer"] for claim in claims
        )
        reference_is_empty = not str(payload.get("reference_answer", "")).strip()
        current_evidence_sufficient = (
            reference_is_empty
            or (
                bool(claims)
                and all(
                    claim["current_side"][field] == "FULL"
                    for claim in claims
                    for field in ("memory_coverage", "retrieval_coverage")
                )
            )
        )
        # Answer remains a residual, exclusive stage.  W2W does not weaken the
        # evidence-sufficiency invariant merely because both runs were wrong.
        answer = (
            answer_requested
            and current_evidence_sufficient
            and not access
            and not construction
        )
        learnable = bool(raw_attr.get("learnable", True)) and bool(
            answer or access or construction
        )
        if not learnable:
            answer = access = construction = False

        failure_to_repair = result.get("failure_to_repair")
        failure_to_repair = (
            failure_to_repair if isinstance(failure_to_repair, dict) else {}
        )
        comparison = payload.get("transition", payload.get("flip", {}))
        core = {
            "schema_version": "persistent_failure_core_v1",
            "case_id": (
                f"persistent_{comparison.get('chain', 'chain')}_"
                f"{payload['qa_id']}"
            ),
            "qa_id": payload["qa_id"],
            "conversation_id": payload.get("conversation_id", ""),
            "question": payload.get("question", ""),
            "reference_answer": payload.get("reference_answer", ""),
            "comparison": comparison,
            "failure_age": max(1, int(payload.get("failure_age", 1))),
            "gold_answer_path": payload.get("gold_answer_path", {}),
            "construction_lineage": payload.get("construction_lineage", {}),
            "repair_lineage": payload.get("repair_lineage", {}),
            "claims": claims,
            "attribution": {
                "answer": answer,
                "access": access,
                "construction": construction,
                "learnable": learnable,
                "confidence": confidence(raw_attr.get("confidence")),
                "reason": str(raw_attr.get("reason") or result.get("reason", "")).strip(),
            },
            "failure_to_repair": {
                "type": str(failure_to_repair.get("type", "UNRESOLVED")).upper(),
                "earliest_divergence": failure_to_repair.get("earliest_divergence"),
                "why_previous_round_failed": str(
                    failure_to_repair.get("why_previous_round_failed", "")
                ).strip(),
            },
            "mechanisms": (
                result.get("mechanisms")
                if isinstance(result.get("mechanisms"), dict)
                else {}
            ),
        }
        return {
            "schema_version": "persistent_failure_diagnosis_v1",
            "problem_found": learnable,
            "reason": core["attribution"]["reason"],
            "confidence": core["attribution"]["confidence"],
            "review_required": bool(result.get("review_required", False)),
            "core": core,
            "projections": self._project(core, payload) if learnable else [],
        }

    @staticmethod
    def _project(core: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        projections: list[dict[str, Any]] = []
        mechanisms = core.get("mechanisms", {})
        prior = payload.get("prior_side", {})
        current = payload.get("current_side", {})
        selected = (current.get("skill_trace") or {}).get("selected", [])
        intent_hint = "REVISE" if selected else "ADD"

        def base(stage: str, side: str) -> dict[str, Any]:
            return {
                "schema_version": f"iteration_{stage}_v1",
                "diagnosis_id": f"{core['case_id']}_{stage}",
                "diagnosis_type": (
                    "CONS_FAILURE" if side == "construction" else "ACCESS_FAILURE"
                ),
                "status": "completed",
                "problem_found": True,
                "source_mode": "iteration",
                "transition": "W2W",
                "flip": core["comparison"],
                "failure_age": core["failure_age"],
                "side": side,
                "stage": stage,
                "qa_id": core["qa_id"],
                "conversation_id": core["conversation_id"],
                "question": core["question"],
                "reference_answer": core["reference_answer"],
                "prediction": str(current.get("answer", "")),
                "learning_polarity": "REPAIR_UNRESOLVED",
                "maintenance_intent_hint": intent_hint,
                "failure_to_repair": core["failure_to_repair"],
                "repair_lineage": core["repair_lineage"],
                "reason": core["attribution"]["reason"],
                "confidence": core["attribution"]["confidence"],
                "review_required": False,
            }

        expected = {
            "reference_answer": core["reference_answer"],
            "gold_answer_path": core["gold_answer_path"],
        }
        if core["attribution"]["answer"]:
            report = base("answer", "access")
            report["skill_trace"] = current.get("skill_trace")
            report["repair_package"] = {
                "stage": "answer",
                "claim_evidence_parity": True,
                "expected_behavior": expected,
                "prior_wrong_behavior": {
                    "answer": prior.get("answer", ""),
                    "final_evidence_ids": prior.get("final_evidence_ids", []),
                },
                "current_wrong_behavior": {
                    "answer": current.get("answer", ""),
                    "final_evidence_ids": current.get("final_evidence_ids", []),
                },
                "mechanism": mechanisms.get("answer", {}),
            }
            projections.append(report)

        if core["attribution"]["access"]:
            report = base("access", "access")
            report["skill_trace"] = current.get("skill_trace")
            report["repair_package"] = {
                "stage": "retrieval",
                "expected_behavior": expected,
                "claim_failures": [
                    claim for claim in core["claims"] if claim["failure"]["access"]
                ],
                "prior_wrong_behavior": {
                    "skill_trace": prior.get("skill_trace"),
                    "search_actions": prior.get("search_actions", []),
                    "visible_memories": prior.get("visible_memories", []),
                },
                "current_wrong_behavior": {
                    "skill_trace": current.get("skill_trace"),
                    "search_actions": current.get("search_actions", []),
                    "visible_memories": current.get("visible_memories", []),
                },
                "mechanism": mechanisms.get("access", {}),
            }
            projections.append(report)

        if core["attribution"]["construction"]:
            report = base("construction", "construction")
            report["construction_skill_traces"] = current.get(
                "construction_skill_traces", []
            )
            report["repair_package"] = {
                "stage": "construction",
                "expected_behavior": expected,
                "source_messages": core["gold_answer_path"].get(
                    "source_messages", []
                ),
                "construction_lineage": core["construction_lineage"],
                "claim_failures": [
                    claim
                    for claim in core["claims"]
                    if claim["failure"]["construction"]
                ],
                "prior_wrong_behavior": {
                    "construction_skill_traces": prior.get(
                        "construction_skill_traces", []
                    ),
                    "construction_traces": prior.get("construction_traces", []),
                    "current_memories": prior.get("current_memories", []),
                },
                "current_wrong_behavior": {
                    "construction_skill_traces": current.get(
                        "construction_skill_traces", []
                    ),
                    "construction_traces": current.get("construction_traces", []),
                    "current_memories": current.get("current_memories", []),
                },
                "earliest_divergence": core["failure_to_repair"].get(
                    "earliest_divergence"
                ),
                "mechanism": mechanisms.get("construction", {}),
            }
            projections.append(report)
        return projections

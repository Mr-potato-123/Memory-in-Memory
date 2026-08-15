"""Diagnosis of useful current memory omitted by the runtime search chain."""

from __future__ import annotations

from typing import Any

from ..diagnosis.model_io import (
    InvalidModelOutput,
    call_json,
    confidence,
    unique_strings,
)
from ..diagnosis.schemas import (
    AccessClaimSupport,
    AccessDiagnosisReport,
    ClaimSupport,
    DiagnosisCase,
    DiagnosisStatus,
    DiagnosisType,
)
from ..llm.base import ModelClient


class AccessFailureAgent:
    """Compare useful current memories with retrieved current memories."""

    def __init__(self, model: ModelClient, *, prompt: str):
        self._model = model
        self._prompt = prompt

    def diagnose(
        self,
        case: DiagnosisCase,
        *,
        current_related_memories: list[dict[str, Any]],
        current_search_steps: list[dict[str, Any]],
        answer_context_sufficient: bool = False,
    ) -> AccessDiagnosisReport:
        report = self._empty_report(
            case,
            current_related_memories=current_related_memories,
            current_search_steps=current_search_steps,
        )
        available_ids = {
            str(item["version_id"])
            for item in current_related_memories
            if item.get("version_id")
        }
        returned_ids = {
            str(version_id)
            for step in current_search_steps
            for version_id in step.get("returned_version_ids", [])
            if version_id
        }
        report.retrieved_current_version_ids = sorted(returned_ids)

        # The answer diagnosis sees the exact runtime context first.  Once it
        # establishes that this context was sufficient, omitted corroborating
        # memories cannot causally explain the wrong answer.
        if answer_context_sufficient:
            report.reason = (
                "Skipped fixed-search diagnosis because the preceding answer "
                "diagnosis established that returned memories were sufficient."
            )
            report.confidence = 1.0
            return report

        # An empty LoCoMo reference denotes an unanswerable/adversarial item.
        # There is no gold fact that Access was required to retrieve.
        if not case.reference_answer.strip():
            report.reason = (
                "Empty reference answer: no required current memory exists "
                "for Access diagnosis."
            )
            report.confidence = 1.0
            return report

        try:
            result = call_json(
                self._model,
                prompt=self._prompt,
                payload={
                    "question": case.question,
                    "reference_answer": case.reference_answer,
                    "snapshot_commit_id": case.snapshot_commit_id,
                    "current_related_memories": current_related_memories,
                    "runtime_current_search_chain": current_search_steps,
                },
                max_tokens=3000,
            )
            claims = self._claims(
                result.get("essential_reference_claims"),
                allowed_ids=available_ids,
                returned_ids=returned_ids,
            )
            useful_ids = sorted(
                {
                    version_id
                    for claim in claims
                    for version_id in claim.supporting_version_ids
                }
            )
            required_missing_ids = sorted({
                version_id
                for claim in claims
                if claim.coverage != "FULL"
                for version_id in claim.supporting_version_ids
                if version_id not in returned_ids
            })
            problem = any(claim.coverage != "FULL" for claim in claims)

            report.claims = claims
            report.useful_current_version_ids = useful_ids
            report.missing_useful_current_version_ids = required_missing_ids
            report.problem_found = problem
            report.diagnosis_type = (
                DiagnosisType.ACCESS_FAILURE
                if problem
                else DiagnosisType.NO_ACCESS_FAILURE
            )
            report.reason = str(result.get("reason", "")).strip()
            report.confidence = confidence(result.get("confidence"))
            report.review_required = bool(
                result.get("review_required", False)
            )
            if problem and not report.review_required:
                memory_by_id = {
                    str(item["version_id"]): item
                    for item in current_related_memories
                    if item.get("version_id")
                }
                report.repair_package = {
                    "schema_version": "mem0_fixed_search_failure_v2",
                    "side": "memory_system",
                    "stage": "mem0_fixed_search",
                    "eligible_for_skill_generation": False,
                    "failure_owner": "mem0_retrieval",
                    "question": case.question,
                    "reference_answer": case.reference_answer,
                    "missing_useful_current_memories": [
                        memory_by_id[version_id]
                        for version_id in required_missing_ids
                    ],
                    "retrieved_current_version_ids": sorted(returned_ids),
                    "search_steps": current_search_steps,
                    "reason": report.reason,
                }
        except Exception as exc:
            report.status = DiagnosisStatus.MODEL_ERROR
            report.reason = f"Access diagnosis failed: {exc}"
            report.review_required = True
        return report

    @staticmethod
    def _claims(
        value: Any,
        *,
        allowed_ids: set[str],
        returned_ids: set[str],
    ) -> list[AccessClaimSupport]:
        if not isinstance(value, list) or not value:
            raise InvalidModelOutput(
                "essential_reference_claims must be a non-empty list."
            )
        claims: list[AccessClaimSupport] = []
        for item in value:
            if not isinstance(item, dict):
                raise InvalidModelOutput("Each claim must be an object.")
            claim = str(item.get("claim", "")).strip()
            if not claim:
                raise InvalidModelOutput("Each claim must contain text.")
            version_ids = unique_strings(
                item.get("supporting_current_version_ids")
            )
            # The model may copy a version from the separately supplied search
            # chain. Only the algorithm-provided related-memory set is eligible
            # evidence for the set difference, so discard all other IDs.
            version_ids = [
                version_id
                for version_id in version_ids
                if version_id in allowed_ids
            ]
            retrieved_supporting_ids = unique_strings(
                item.get("supporting_retrieved_version_ids")
            )
            retrieved_supporting_ids = [
                version_id
                for version_id in retrieved_supporting_ids
                if version_id in allowed_ids and version_id in returned_ids
            ]
            try:
                coverage = str(item.get("retrieval_coverage", "")).upper()
                if coverage not in {"FULL", "PARTIAL", "MISSING", "INCORRECT"}:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise InvalidModelOutput(
                    "Each access claim must declare retrieval_coverage as "
                    "FULL, PARTIAL, MISSING, or INCORRECT."
                ) from exc
            claims.append(
                AccessClaimSupport(
                    claim=claim,
                    supporting_version_ids=version_ids,
                    retrieved_supporting_version_ids=retrieved_supporting_ids,
                    coverage=coverage,
                )
            )
        return claims

    @staticmethod
    def _empty_report(
        case: DiagnosisCase,
        *,
        current_related_memories: list[dict[str, Any]],
        current_search_steps: list[dict[str, Any]],
    ) -> AccessDiagnosisReport:
        return AccessDiagnosisReport(
            diagnosis_id=f"access_{case.conversation_id}_{case.qa_id}",
            diagnosis_type=DiagnosisType.NO_ACCESS_FAILURE,
            judge_run_id=case.judge_run_id,
            diagnosis_run_id=case.diagnosis_run_id,
            source_runtime_run=case.source_runtime_run,
            conversation_id=case.conversation_id,
            qa_id=case.qa_id,
            snapshot_commit_id=case.snapshot_commit_id,
            question=case.question,
            reference_answer=case.reference_answer,
            access_run_id=case.access_run_id,
            current_related_memories=current_related_memories,
            search_steps=current_search_steps,
            repair_package=None,
        )

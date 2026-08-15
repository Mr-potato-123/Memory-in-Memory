"""Diagnosis of answer errors made with sufficient retrieved context."""

from __future__ import annotations

from typing import Any

from ..diagnosis.model_io import (
    InvalidModelOutput,
    call_json,
    confidence,
    require_known_ids,
    unique_strings,
)
from ..diagnosis.schemas import (
    AnswerDiagnosisReport,
    ClaimCoverage,
    ClaimSupport,
    DiagnosisCase,
    DiagnosisStatus,
    DiagnosisType,
)
from ..llm.base import ModelClient


class AnswerFailureAgent:
    """Judge context sufficiency without re-answering the question."""

    def __init__(self, model: ModelClient, *, prompt: str):
        self._model = model
        self._prompt = prompt

    def diagnose(
        self,
        case: DiagnosisCase,
        *,
        exact_search_steps: list[dict[str, Any]],
    ) -> AnswerDiagnosisReport:
        report = self._empty_report(case, exact_search_steps)
        allowed_ids = {
            str(version_id)
            for step in exact_search_steps
            for version_id in step.get("returned_version_ids", [])
            if version_id
        }
        report.retrieved_version_ids = sorted(allowed_ids)

        try:
            result = call_json(
                self._model,
                prompt=self._prompt,
                payload={
                    "question": case.question,
                    "reference_answer": case.reference_answer,
                    "reference_answer_is_empty": not bool(
                        case.reference_answer.strip()
                    ),
                    "runtime_prediction": case.prediction,
                    "judge": {
                        "label": case.judge_label,
                        "reason": case.judge_reason,
                    },
                    "runtime_search_chain": exact_search_steps,
                },
                max_tokens=3000,
            )
            claims = self._claims(
                result.get("essential_reference_claims"),
                id_key="supporting_retrieved_version_ids",
                allowed_ids=allowed_ids,
                allow_empty=not bool(case.reference_answer.strip()),
            )
            contradiction = bool(
                result.get("unresolved_material_contradiction", False)
            )
            if case.reference_answer.strip():
                sufficient = (
                    bool(claims)
                    and all(
                        claim.coverage == ClaimCoverage.FULL
                        and claim.supporting_version_ids
                        for claim in claims
                    )
                    and not contradiction
                )
            else:
                sufficient = (
                    bool(result.get("retrieved_context_supports_abstention"))
                    and not contradiction
                )
            problem = case.judge_label in {"P", "I", "W"} and sufficient
            failure_mode = str(result.get("failure_mode", "OTHER")).strip().upper()
            observable_trigger = str(
                result.get("observable_trigger", "")
            ).strip()
            corrective_operation = str(
                result.get("corrective_operation", "")
            ).strip()
            requested_learnable = bool(result.get("skill_learnable", False))
            skill_learnable = bool(
                problem
                and requested_learnable
                and observable_trigger
                and corrective_operation
                and failure_mode not in {
                    "GENERIC_INSTRUCTION_FOLLOWING",
                    "OTHER",
                }
            )

            report.claims = claims
            report.unresolved_material_contradiction = contradiction
            report.failure_mode = failure_mode
            report.skill_learnable = skill_learnable
            report.observable_trigger = observable_trigger
            report.corrective_operation = corrective_operation
            report.retrieved_context_sufficient = sufficient
            report.problem_found = problem
            report.diagnosis_type = (
                DiagnosisType.ANSWER_FAILURE
                if problem
                else DiagnosisType.NO_ANSWER_FAILURE
            )
            report.reason = str(result.get("reason", "")).strip()
            report.confidence = confidence(result.get("confidence"))
            report.review_required = bool(
                result.get("review_required", False)
            )
            if problem and not report.review_required:
                # Answer behaviour belongs to the Access & Answer Agent. Keep
                # this package small: the full report remains the audit
                # artifact, while candidate generation sees only the evidence
                # needed to internalize a reusable answering rule.
                report.repair_package = {
                    "schema_version": "mem0_answer_failure_v2",
                    "source_mode": "standard",
                    "side": "access",
                    "stage": "answer",
                    "question": case.question,
                    "reference_answer": case.reference_answer,
                    "runtime_prediction": case.prediction,
                    "retrieved_context_sufficient": True,
                    "eligible_for_skill_generation": skill_learnable,
                    "failure_scope": (
                        "memory_answering_procedure"
                        if skill_learnable
                        else "record_only_answer_failure"
                    ),
                    "failure_mode": failure_mode,
                    "observable_trigger": observable_trigger,
                    "corrective_operation": corrective_operation,
                    "essential_reference_claims": [
                        claim.model_dump(mode="json") for claim in claims
                    ],
                    "retrieved_version_ids": sorted(allowed_ids),
                    "search_steps": exact_search_steps,
                    "unresolved_material_contradiction": contradiction,
                    "reason": report.reason,
                }
        except Exception as exc:
            report.status = DiagnosisStatus.MODEL_ERROR
            report.reason = f"Answer diagnosis failed: {exc}"
            report.review_required = True
        return report

    @staticmethod
    def _claims(
        value: Any,
        *,
        id_key: str,
        allowed_ids: set[str],
        allow_empty: bool = False,
    ) -> list[ClaimSupport]:
        if allow_empty and (value is None or value == []):
            return []
        if not isinstance(value, list) or not value:
            raise InvalidModelOutput(
                "essential_reference_claims must be a non-empty list."
            )
        claims: list[ClaimSupport] = []
        for item in value:
            if not isinstance(item, dict):
                raise InvalidModelOutput("Each claim must be an object.")
            claim = str(item.get("claim", "")).strip()
            if not claim:
                raise InvalidModelOutput("Each claim must contain text.")
            version_ids = unique_strings(item.get(id_key))
            require_known_ids(version_ids, allowed_ids, id_key)
            raw_coverage = item.get("coverage")
            if raw_coverage is None or not str(raw_coverage).strip():
                raise InvalidModelOutput(
                    "Each answer claim must declare coverage as "
                    "FULL, PARTIAL, MISSING, or INCORRECT."
                )
            try:
                coverage = ClaimCoverage(str(raw_coverage).strip().upper())
            except ValueError as exc:
                raise InvalidModelOutput(
                    "Claim coverage must be FULL, PARTIAL, MISSING, "
                    "or INCORRECT."
                ) from exc
            claims.append(
                ClaimSupport(
                    claim=claim,
                    supporting_version_ids=version_ids,
                    coverage=coverage,
                )
            )
        return claims

    @staticmethod
    def _empty_report(
        case: DiagnosisCase,
        search_steps: list[dict[str, Any]],
    ) -> AnswerDiagnosisReport:
        return AnswerDiagnosisReport(
            diagnosis_id=f"answer_{case.conversation_id}_{case.qa_id}",
            diagnosis_type=DiagnosisType.NO_ANSWER_FAILURE,
            judge_run_id=case.judge_run_id,
            diagnosis_run_id=case.diagnosis_run_id,
            source_runtime_run=case.source_runtime_run,
            conversation_id=case.conversation_id,
            qa_id=case.qa_id,
            snapshot_commit_id=case.snapshot_commit_id,
            question=case.question,
            reference_answer=case.reference_answer,
            prediction=case.prediction,
            search_steps=search_steps,
            repair_package=None,
        )

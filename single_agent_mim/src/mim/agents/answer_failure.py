"""Record-only diagnosis of errors made with sufficient retrieved context."""

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
                    and all(claim.supporting_version_ids for claim in claims)
                    and not contradiction
                )
            else:
                sufficient = (
                    bool(result.get("retrieved_context_supports_abstention"))
                    and not contradiction
                )
            problem = case.judge_label in {"P", "I", "W"} and sufficient

            report.claims = claims
            report.unresolved_material_contradiction = contradiction
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
            claims.append(
                ClaimSupport(
                    claim=claim,
                    supporting_version_ids=version_ids,
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

"""Two-stage diagnosis of the earliest memory-construction failure."""

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
    ClaimCoverage,
    ClaimSupport,
    ConsDiagnosisReport,
    ConsScreeningReport,
    DiagnosisCase,
    DiagnosisStatus,
    DiagnosisType,
)
from ..llm.base import ModelClient


class ConsFailureAgent:
    """Screen current memory first, then trace only confirmed candidates."""

    _STAGE_ALIASES = {
        "candidate_generation": "extraction",
        "candidate": "extraction",
        "wrong_candidate": "extraction",
        "wrong_skip": "decision",
        "persistence": "initial_memory",
        "update_loss": "update",
        "wrong_merge": "update",
        "correction_failure": "update",
        "merge": "update",
    }
    _VALID_STAGES = {
        "ingestion",
        "extraction",
        "decision",
        "initial_memory",
        "update",
    }

    def __init__(
        self,
        model: ModelClient,
        *,
        screening_prompt: str,
        trace_prompt: str,
    ):
        self._model = model
        self._screening_prompt = screening_prompt
        self._trace_prompt = trace_prompt

    def screen(
        self,
        case: DiagnosisCase,
        *,
        current_related_memories: list[dict[str, Any]],
    ) -> ConsScreeningReport:
        report = ConsScreeningReport()
        # Empty LoCoMo references are unanswerable/adversarial. There is no
        # gold claim whose construction provenance can be traced.
        if not case.reference_answer.strip():
            report.reason = (
                "Empty reference answer: no gold claim enters Construction "
                "diagnosis."
            )
            report.confidence = 1.0
            return report
        allowed_ids = {
            str(item["version_id"])
            for item in current_related_memories
            if item.get("version_id")
        }
        try:
            result = call_json(
                self._model,
                prompt=self._screening_prompt,
                payload={
                    "question": case.question,
                    "reference_answer": case.reference_answer,
                    "snapshot_commit_id": case.snapshot_commit_id,
                    "current_related_memories": current_related_memories,
                },
                max_tokens=3000,
            )
            claims = self._screening_claims(
                result.get("essential_reference_claims"),
                allowed_ids=allowed_ids,
            )
            report.claims = claims
            report.cons_candidate = any(
                claim.coverage != ClaimCoverage.FULL
                for claim in claims
            )
            report.reason = str(result.get("reason", "")).strip()
            report.confidence = confidence(result.get("confidence"))
            report.review_required = bool(
                result.get("review_required", False)
            )
        except Exception as exc:
            report.status = DiagnosisStatus.MODEL_ERROR
            report.reason = f"Cons screening failed: {exc}"
            report.review_required = True
        return report

    def trace(
        self,
        case: DiagnosisCase,
        *,
        screening: ConsScreeningReport,
        current_related_memories: list[dict[str, Any]],
        source_messages: list[dict[str, Any]],
        construction_history: dict[str, Any],
    ) -> ConsDiagnosisReport:
        report = self._empty_report(case, screening)
        report.source_message_ids = [
            str(item["message_id"])
            for item in source_messages
            if item.get("message_id")
        ]

        try:
            result = call_json(
                self._model,
                prompt=self._trace_prompt,
                payload={
                    "question": case.question,
                    "reference_answer": case.reference_answer,
                    "screening": screening.model_dump(mode="json"),
                    "annotated_raw_evidence": source_messages,
                    "current_related_memories": current_related_memories,
                    "chronological_construction_history": construction_history,
                },
                max_tokens=5000,
            )
            raw_support = str(
                result.get("raw_support", "INVALID")
            ).upper().strip()
            report.raw_support = raw_support
            report.reason = str(result.get("reason", "")).strip()
            report.confidence = confidence(result.get("confidence"))
            report.review_required = bool(
                result.get("review_required", False)
            )

            if raw_support == "INVALID":
                # Evidence is unusable (missing/contradictory at the source):
                # no construction error can be attributed. Drop the case.
                report.status = DiagnosisStatus.DATA_ERROR
                report.review_required = True
                return report
            if raw_support != "SUPPORTED":
                # PARTIAL / CONTRADICTORY: the raw evidence only partially
                # supports (or conflicts with) the reference. The trace stage
                # still inspects construction history, so keep diagnosing but
                # flag the case for human review — a PARTIAL verdict often
                # masks an extraction/date-resolution bug that IS a genuine
                # construction error (e.g. 'yesterday' → wrong date).
                report.review_required = True

            construction_problem = bool(
                result.get("construction_problem", True)
            )
            if not construction_problem:
                return report

            first_error = self._validated_first_error(
                result.get("first_error"),
                source_messages=source_messages,
                construction_history=construction_history,
            )
            affected_memory_ids = unique_strings(
                result.get("affected_memory_ids")
            )
            valid_memory_ids = self._valid_memory_ids(construction_history)
            require_known_ids(
                affected_memory_ids,
                valid_memory_ids,
                "affected_memory_ids",
            )

            report.problem_found = True
            report.diagnosis_type = DiagnosisType.CONS_FAILURE
            report.primary_subtype = str(
                result.get("subtype") or first_error["stage"]
            ).strip().lower()
            report.first_error = first_error
            report.affected_memory_ids = affected_memory_ids
            report.construction_history = construction_history
            # A valid first-error attribution always yields a repair package.
            # review_required only flags confidence for later human review —
            # it must not discard a diagnosis that already located the
            # construction error (e.g. PARTIAL raw_support cases).
            if first_error.get("stage"):
                report.repair_package = {
                    "question": case.question,
                    "reference_answer": case.reference_answer,
                    "affected_reference_claim": str(
                        result.get("affected_reference_claim", "")
                    ).strip(),
                    "source_messages": source_messages,
                    "affected_memory_ids": affected_memory_ids,
                    "first_error": first_error,
                    "reason": report.reason,
                    "relevant_history": self._history_through_first_error(
                        construction_history,
                        first_error,
                    ),
                }
        except Exception as exc:
            report.status = DiagnosisStatus.MODEL_ERROR
            report.reason = f"Cons trace failed: {exc}"
            report.review_required = True
        return report

    @staticmethod
    def no_problem(
        case: DiagnosisCase,
        screening: ConsScreeningReport,
    ) -> ConsDiagnosisReport:
        return ConsFailureAgent._empty_report(case, screening)

    @staticmethod
    def _screening_claims(
        value: Any,
        *,
        allowed_ids: set[str],
    ) -> list[ClaimSupport]:
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
            version_ids = unique_strings(
                item.get("supporting_current_version_ids")
            )
            require_known_ids(
                version_ids,
                allowed_ids,
                "supporting_current_version_ids",
            )
            try:
                coverage = ClaimCoverage(
                    str(item.get("coverage", "")).upper()
                )
            except ValueError as exc:
                raise InvalidModelOutput(
                    f"Invalid claim coverage for {claim!r}."
                ) from exc
            claims.append(
                ClaimSupport(
                    claim=claim,
                    supporting_version_ids=version_ids,
                    coverage=coverage,
                )
            )
        return claims

    def _validated_first_error(
        self,
        value: Any,
        *,
        source_messages: list[dict[str, Any]],
        construction_history: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise InvalidModelOutput("first_error must be an object.")
        raw_stage = str(value.get("stage", "")).strip().lower()
        stage = self._STAGE_ALIASES.get(raw_stage, raw_stage)
        if stage not in self._VALID_STAGES:
            raise InvalidModelOutput(f"Unknown first-error stage: {raw_stage}")

        valid_message_ids = {
            str(item["message_id"])
            for item in source_messages
            if item.get("message_id")
        }
        # A gold-supported memory may have been modified later by other
        # messages. Those history-linked messages are valid provenance and
        # are essential for locating an update-time loss.
        for candidate in construction_history.get("candidates", []):
            if candidate.get("message_id"):
                valid_message_ids.add(str(candidate["message_id"]))
        for change in construction_history.get("change_events", []):
            valid_message_ids.update(
                str(message_id)
                for field in ("direct_message_ids", "affected_message_ids")
                for message_id in change.get(field, [])
                if message_id
            )
        for memory in construction_history.get("snapshot_memories", []):
            valid_message_ids.update(
                str(message_id)
                for message_id in memory.get("source_message_ids", [])
                if message_id
            )
        valid_candidate_ids = {
            str(item["candidate_id"])
            for item in construction_history.get("candidates", [])
            if item.get("candidate_id")
        }
        valid_decision_ids = {
            str(item["decision_id"])
            for item in construction_history.get("candidates", [])
            if item.get("decision_id")
        }
        valid_decision_ids.update(
            str(item["decision_id"])
            for item in construction_history.get("change_events", [])
            if item.get("decision_id")
        )
        valid_change_ids = {
            str(item["change_id"])
            for item in construction_history.get("change_events", [])
            if item.get("change_id")
        }
        valid_version_ids = self._valid_version_ids(construction_history)
        valid_commits = {
            int(item["commit_id"])
            for group in (
                construction_history.get("processed_commits", []),
                construction_history.get("candidates", []),
                construction_history.get("change_events", []),
            )
            for item in group
            if item.get("commit_id") is not None
        }

        message_ids = unique_strings(value.get("message_ids"))
        before_ids = unique_strings(value.get("before_version_ids"))
        require_known_ids(message_ids, valid_message_ids, "message_ids")
        require_known_ids(
            before_ids,
            valid_version_ids,
            "before_version_ids",
        )

        candidate_id = self._optional_known(
            value.get("candidate_id"),
            valid_candidate_ids,
            "candidate_id",
        )
        decision_id = self._optional_known(
            value.get("decision_id"),
            valid_decision_ids,
            "decision_id",
        )
        change_id = self._optional_known(
            value.get("change_id"),
            valid_change_ids,
            "change_id",
        )
        after_version_id = self._optional_known(
            value.get("after_version_id"),
            valid_version_ids,
            "after_version_id",
        )
        if stage == "update" and change_id:
            matching_change = next(
                (
                    item
                    for item in construction_history.get("change_events", [])
                    if str(item.get("change_id", "")) == change_id
                ),
                None,
            )
            if matching_change is not None:
                if not before_ids:
                    before_ids = [
                        str(item["version_id"])
                        for item in matching_change.get("before_versions", [])
                        if item.get("version_id")
                    ]
                if not after_version_id:
                    after = matching_change.get("after_version")
                    if isinstance(after, dict) and after.get("version_id"):
                        after_version_id = str(after["version_id"])
        commit_id = value.get("commit_id")
        if commit_id is not None:
            try:
                commit_id = int(commit_id)
            except (TypeError, ValueError) as exc:
                raise InvalidModelOutput("commit_id must be an integer.") from exc
            if commit_id not in valid_commits:
                raise InvalidModelOutput(
                    f"commit_id was not supplied: {commit_id}"
                )
        if stage == "update" and (not before_ids or not after_version_id):
            raise InvalidModelOutput(
                "An update error requires verified before and after versions."
            )

        return {
            "stage": stage,
            "message_ids": message_ids,
            "candidate_id": candidate_id,
            "decision_id": decision_id,
            "commit_id": commit_id,
            "change_id": change_id,
            "operation": (
                str(value["operation"])
                if value.get("operation") is not None
                else None
            ),
            "before_version_ids": before_ids,
            "after_version_id": after_version_id,
        }

    @staticmethod
    def _optional_known(
        value: Any,
        allowed: set[str],
        field: str,
    ) -> str | None:
        if value is None or not str(value).strip():
            return None
        normalized = str(value)
        require_known_ids([normalized], allowed, field)
        return normalized

    @staticmethod
    def _valid_version_ids(history: dict[str, Any]) -> set[str]:
        ids = {
            str(item["version_id"])
            for item in history.get("snapshot_memories", [])
            if item.get("version_id")
        }
        for change in history.get("change_events", []):
            ids.update(
                str(item["version_id"])
                for item in change.get("before_versions", [])
                if item.get("version_id")
            )
            after = change.get("after_version")
            if isinstance(after, dict) and after.get("version_id"):
                ids.add(str(after["version_id"]))
        return ids

    @staticmethod
    def _valid_memory_ids(history: dict[str, Any]) -> set[str]:
        ids = {
            str(item["memory_id"])
            for item in history.get("snapshot_memories", [])
            if item.get("memory_id")
        }
        for change in history.get("change_events", []):
            ids.update(
                str(item["memory_id"])
                for item in change.get("before_versions", [])
                if item.get("memory_id")
            )
            after = change.get("after_version")
            if isinstance(after, dict) and after.get("memory_id"):
                ids.add(str(after["memory_id"]))
        return ids

    @staticmethod
    def _history_through_first_error(
        history: dict[str, Any],
        first_error: dict[str, Any],
    ) -> dict[str, Any]:
        commit_id = first_error.get("commit_id")
        if commit_id is None:
            return history

        def before_or_at(item: dict[str, Any]) -> bool:
            value = item.get("commit_id")
            return value is None or int(value) <= int(commit_id)

        return {
            "processed_commits": [
                item
                for item in history.get("processed_commits", [])
                if before_or_at(item)
            ],
            "candidates": [
                item
                for item in history.get("candidates", [])
                if before_or_at(item)
            ],
            "change_events": [
                item
                for item in history.get("change_events", [])
                if before_or_at(item)
            ],
            "snapshot_memories": history.get("snapshot_memories", []),
        }

    @staticmethod
    def _empty_report(
        case: DiagnosisCase,
        screening: ConsScreeningReport,
    ) -> ConsDiagnosisReport:
        return ConsDiagnosisReport(
            diagnosis_id=f"cons_{case.conversation_id}_{case.qa_id}",
            diagnosis_type=DiagnosisType.NO_CONS_FAILURE,
            judge_run_id=case.judge_run_id,
            diagnosis_run_id=case.diagnosis_run_id,
            source_runtime_run=case.source_runtime_run,
            conversation_id=case.conversation_id,
            qa_id=case.qa_id,
            snapshot_commit_id=case.snapshot_commit_id,
            question=case.question,
            reference_answer=case.reference_answer,
            screening=screening,
            reason=screening.reason,
            confidence=screening.confidence,
            review_required=screening.review_required,
            repair_package=None,
        )

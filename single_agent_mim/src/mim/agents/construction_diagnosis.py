"""Append-only memory-extraction diagnosis for one failed QA.

The agent receives only annotated raw evidence (plus explicitly resolved local
context when supplied) and a deterministic chronological construction history.
It reports at most the earliest construction error.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..failure.schemas import (
    ConstructionDiagnosisReport,
    DiagnosisStatus,
    LearningRoute,
)
from ..llm.base import ModelClient


CONSTRUCTION_DIAGNOSIS_PROMPT = """\
You diagnose only memory construction for a failed question.

First verify whether the annotated raw messages support the reference answer.
Then inspect the chronological construction history: processing, candidates,
decisions (including SKIP), created versions, and every later change affecting
those source messages.

If memory construction is wrong, report only the earliest error. Compare the
raw claim with the candidate, then the first version, then every before/after
change in order. Do not diagnose retrieval.

Return one JSON object:
{
  "raw_support": "SUPPORTED|PARTIAL|CONTRADICTORY|INVALID",
  "construction_problem": true,
  "subtype": "ingestion|extraction|wrong_candidate|wrong_skip|persistence|initial_memory|update_loss|wrong_merge|correction_failure|provenance_missing|none",
  "first_error": {
    "stage": "",
    "message_ids": [],
    "candidate_id": null,
    "decision_id": null,
    "commit_id": null,
    "operation": null,
    "before_version_ids": [],
    "after_version_id": null
  },
  "reason": "plain explanation of the first error",
  "confidence": 0.0,
  "review_required": false
}

Do not report a later error when an earlier one already explains the loss.
Do not invent IDs.
"""


class ConstructionDiagnosisAgent:
    """Locate the earliest extraction or deterministic persistence error."""

    _STAGE_TO_EDGE = {
        "ingestion": "message_to_construction",
        "extraction_omission": "message_to_candidate",
        "extraction_distortion": "message_to_candidate",
        "temporal_metadata": "message_to_candidate",
        "persistence": "decision_to_version",
        "provenance_missing": "source_to_version_trace",
    }
    _STAGE_ALIASES = {
        "candidate_generation": "extraction_omission",
        "candidate": "extraction_omission",
        "message_to_candidate": "extraction_omission",
        "extraction": "extraction_omission",
        "wrong_candidate": "extraction_distortion",
        "initial_memory": "persistence",
    }
    _LEARNABLE_STAGES = {
        "extraction_omission",
        "extraction_distortion",
        "temporal_metadata",
    }

    def __init__(
        self,
        model: ModelClient,
        *,
        prompt: str = CONSTRUCTION_DIAGNOSIS_PROMPT,
    ):
        self._model = model
        self._prompt = prompt

    def diagnose(
        self,
        *,
        failure_id: str,
        run_id: str,
        conversation_id: str,
        qa_id: str,
        snapshot_commit_id: int,
        question: str,
        prediction: str,
        reference_answer: str,
        raw_message_ids: list[str],
        source_messages: list[dict[str, Any]],
        construction_history: dict[str, Any],
    ) -> ConstructionDiagnosisReport:
        report = ConstructionDiagnosisReport(
            failure_id=f"{failure_id}_construction",
            run_id=run_id,
            conversation_id=conversation_id,
            qa_id=qa_id,
            snapshot_commit_id=snapshot_commit_id,
            question=question,
            prediction=prediction,
            reference_answer=reference_answer,
            raw_message_ids=raw_message_ids,
            source_messages=source_messages,
            construction_history=construction_history,
        )

        if not source_messages:
            report.status = DiagnosisStatus.ENGINEERING_ISSUE
            report.primary_subtype = "source_trace_missing"
            report.reason = "Annotated source message IDs could not be resolved."
            report.review_required = True
            report.recommended_route = LearningRoute.ENGINEERING_ISSUE
            return report

        payload = {
            "question": question,
            "reference_answer": reference_answer,
            "annotated_source_messages": source_messages,
            "chronological_construction_history": construction_history,
        }
        try:
            response = self._model.generate(
                [
                    {"role": "system", "content": self._prompt},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                temperature=0.0,
                max_tokens=2200,
                json_mode=True,
            )
            judgment = self._parse_json(response.text)
        except Exception as exc:
            report.status = DiagnosisStatus.MODEL_ERROR
            report.reason = f"Construction diagnosis model call failed: {exc}"
            report.review_required = True
            return report

        if not judgment:
            report.status = DiagnosisStatus.MODEL_ERROR
            report.reason = "Construction diagnosis returned invalid JSON."
            report.review_required = True
            return report

        raw_support = str(
            judgment.get("raw_support", "INVALID")
        ).upper().strip()
        if raw_support not in {
            "SUPPORTED", "PARTIAL", "CONTRADICTORY", "INVALID"
        }:
            raw_support = "INVALID"
        report.raw_support = raw_support
        report.reason = str(judgment.get("reason", "")).strip()
        report.confidence = self._confidence(judgment.get("confidence"))
        report.review_required = bool(judgment.get("review_required", False))

        if raw_support != "SUPPORTED":
            report.status = DiagnosisStatus.DATA_ISSUE
            report.primary_subtype = f"raw_{raw_support.lower()}"
            report.review_required = raw_support == "PARTIAL"
            return report

        subtype = str(judgment.get("subtype", "none")).strip().lower()
        first_error = judgment.get("first_error")
        if not isinstance(first_error, dict):
            first_error = {}
        first_error = self._normalize_first_error(
            first_error,
            raw_message_ids=raw_message_ids,
            construction_history=construction_history,
        )
        raw_stage = str(first_error.get("stage") or subtype).strip().lower()
        stage = self._STAGE_ALIASES.get(raw_stage, raw_stage)
        if stage not in self._STAGE_TO_EDGE and subtype in self._STAGE_TO_EDGE:
            stage = subtype
        first_error["stage"] = stage

        report.problem_found = bool(
            judgment.get("construction_problem", False)
        ) and subtype != "none"
        report.primary_subtype = subtype if report.problem_found else (
            "no_construction_problem"
        )
        report.first_broken_edge = (
            self._STAGE_TO_EDGE.get(stage)
            if report.problem_found
            else None
        )
        report.first_error = first_error if report.problem_found else {}

        if report.problem_found and stage not in self._STAGE_TO_EDGE:
            report.review_required = True
            report.reason = (
                report.reason
                + " The reported first-error stage was not recognized."
            ).strip()

        if (
            report.problem_found
            and not report.review_required
            and stage in self._LEARNABLE_STAGES
        ):
            report.recommended_route = LearningRoute.CONSTRUCTION_SKILL_MAKER
            report.repair_package = {
                "schema_version": "append_only_extraction_repair_v1",
                "learnable_stage": stage,
                "question": question,
                "source_messages": source_messages,
                "first_error": first_error,
                "reason": report.reason,
                "relevant_history": self._history_for_first_error(
                    construction_history, first_error
                ),
            }
        elif report.problem_found and stage not in self._LEARNABLE_STAGES:
            report.status = DiagnosisStatus.ENGINEERING_ISSUE
            report.recommended_route = LearningRoute.ENGINEERING_ISSUE
        return report

    @staticmethod
    def _normalize_first_error(
        first_error: dict[str, Any],
        *,
        raw_message_ids: list[str],
        construction_history: dict[str, Any],
    ) -> dict[str, Any]:
        candidates = construction_history.get("candidates", [])
        changes = construction_history.get("change_events", [])
        valid_candidate_ids = {
            str(item.get("candidate_id"))
            for item in candidates if item.get("candidate_id")
        }
        valid_decision_ids = {
            str(item.get("decision_id"))
            for item in candidates if item.get("decision_id")
        }
        valid_commits = {
            int(item["commit_id"])
            for item in (
                construction_history.get("processed_commits", [])
                + candidates
                + changes
            )
            if item.get("commit_id") is not None
        }
        valid_versions = {
            str(item.get("version_id"))
            for item in construction_history.get("snapshot_memories", [])
            if item.get("version_id")
        }
        for change in changes:
            for before in change.get("before_versions", []):
                if before.get("version_id"):
                    valid_versions.add(str(before["version_id"]))
            after = change.get("after_version")
            if isinstance(after, dict) and after.get("version_id"):
                valid_versions.add(str(after["version_id"]))

        candidate_id = first_error.get("candidate_id")
        decision_id = first_error.get("decision_id")
        commit_id = first_error.get("commit_id")
        try:
            commit_id = int(commit_id) if commit_id is not None else None
        except (TypeError, ValueError):
            commit_id = None

        return {
            "stage": str(first_error.get("stage", "")).strip().lower(),
            "message_ids": [
                str(item)
                for item in first_error.get("message_ids", [])
                if str(item) in set(raw_message_ids)
            ] if isinstance(first_error.get("message_ids"), list) else [],
            "candidate_id": (
                str(candidate_id)
                if candidate_id is not None
                and str(candidate_id) in valid_candidate_ids
                else None
            ),
            "decision_id": (
                str(decision_id)
                if decision_id is not None
                and str(decision_id) in valid_decision_ids
                else None
            ),
            "commit_id": commit_id if commit_id in valid_commits else None,
            "operation": (
                str(first_error.get("operation"))
                if first_error.get("operation") is not None
                else None
            ),
            "before_version_ids": [
                str(item)
                for item in first_error.get("before_version_ids", [])
                if str(item) in valid_versions
            ] if isinstance(first_error.get("before_version_ids"), list) else [],
            "after_version_id": (
                str(first_error.get("after_version_id"))
                if first_error.get("after_version_id") is not None
                and str(first_error.get("after_version_id")) in valid_versions
                else None
            ),
        }

    @staticmethod
    def _history_for_first_error(
        history: dict[str, Any],
        first_error: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_id = first_error.get("candidate_id")
        decision_id = first_error.get("decision_id")
        commit_id = first_error.get("commit_id")
        return {
            "processed_commits": history.get("processed_commits", []),
            "candidates": [
                item
                for item in history.get("candidates", [])
                if (
                    not candidate_id
                    or item.get("candidate_id") == candidate_id
                )
            ],
            "change_events": [
                item
                for item in history.get("change_events", [])
                if (
                    (decision_id and item.get("decision_id") == decision_id)
                    or (commit_id is not None and item.get("commit_id") == commit_id)
                )
            ],
        }

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.5

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

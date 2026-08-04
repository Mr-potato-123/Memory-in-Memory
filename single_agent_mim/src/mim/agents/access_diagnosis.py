"""Retrieval-side diagnosis for one failed QA.

This agent never reads raw conversation messages. The program resolves
gold-source IDs to snapshot memories and supplies the exact natural search
chain, step by step.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..failure.schemas import (
    AccessDiagnosisReport,
    DiagnosisStatus,
    LearningRoute,
)
from ..llm.base import ModelClient


ACCESS_DIAGNOSIS_PROMPT = """\
You diagnose only retrieval for a failed memory question.

You receive:
1. the question and reference answer;
2. memory versions that existed at the frozen snapshot and descend from the
   dataset's annotated evidence messages;
3. every action in the natural search chain and the complete data returned by
   each action.

Do not ask for or infer from raw conversation text. Decide which available
memory versions are actually necessary for the answer, then check whether each
was returned in any search step. All returned items stayed in the answer
model's context.

Return one JSON object:
{
  "necessary_available_version_ids": [],
  "conflicting_returned_version_ids": [],
  "reason": "plain, concise explanation",
  "confidence": 0.0,
  "review_required": false
}

Do not invent version IDs. Do not generate retrieval weights, filters, scores,
or a replacement query. A construction defect does not cancel a retrieval
defect: judge every useful memory that actually existed.
"""


class AccessDiagnosisAgent:
    """Judge whether useful snapshot memories were returned by the search chain."""

    def __init__(
        self,
        model: ModelClient,
        *,
        prompt: str = ACCESS_DIAGNOSIS_PROMPT,
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
        access_run_id: str,
        snapshot_commit_id: int,
        question: str,
        prediction: str,
        reference_answer: str,
        relevant_snapshot_memories: list[dict[str, Any]],
        search_steps: list[dict[str, Any]],
    ) -> AccessDiagnosisReport:
        report = AccessDiagnosisReport(
            failure_id=f"{failure_id}_access",
            run_id=run_id,
            conversation_id=conversation_id,
            qa_id=qa_id,
            access_run_id=access_run_id,
            snapshot_commit_id=snapshot_commit_id,
            question=question,
            prediction=prediction,
            reference_answer=reference_answer,
            relevant_snapshot_memories=relevant_snapshot_memories,
            search_steps=search_steps,
        )

        payload = {
            "question": question,
            "reference_answer": reference_answer,
            "relevant_snapshot_memories": relevant_snapshot_memories,
            "natural_search_chain": search_steps,
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
                max_tokens=1800,
                json_mode=True,
            )
            judgment = self._parse_json(response.text)
        except Exception as exc:
            report.status = DiagnosisStatus.MODEL_ERROR
            report.reason = f"Retrieval diagnosis model call failed: {exc}"
            report.review_required = True
            return report

        if not judgment:
            report.status = DiagnosisStatus.MODEL_ERROR
            report.reason = "Retrieval diagnosis returned invalid JSON."
            report.review_required = True
            return report

        available_ids = {
            str(item.get("version_id"))
            for item in relevant_snapshot_memories
            if item.get("version_id")
        }
        returned_ids = {
            str(version_id)
            for step in search_steps
            for version_id in step.get("returned_version_ids", [])
            if version_id
        }

        requested_necessary = self._string_list(
            judgment.get("necessary_available_version_ids")
        )
        necessary = [
            version_id
            for version_id in requested_necessary
            if version_id in available_ids
        ]
        returned_necessary = [
            version_id for version_id in necessary if version_id in returned_ids
        ]
        missing = [
            version_id for version_id in necessary if version_id not in returned_ids
        ]
        conflicting = [
            version_id
            for version_id in self._string_list(
                judgment.get("conflicting_returned_version_ids")
            )
            if version_id in returned_ids
        ]

        report.necessary_available_version_ids = necessary
        report.returned_necessary_version_ids = returned_necessary
        report.missing_necessary_version_ids = missing
        report.conflicting_returned_version_ids = conflicting
        report.search_steps = self._annotate_search_steps(
            search_steps, set(necessary)
        )
        # Access repair is strictly about missing necessary available memory.
        # Conflicting results remain useful audit context but do not by
        # themselves create an Access Skill-Maker route.
        report.problem_found = bool(missing)
        report.reason = str(judgment.get("reason", "")).strip()
        report.confidence = self._confidence(judgment.get("confidence"))
        report.review_required = bool(judgment.get("review_required", False))

        if missing:
            report.primary_subtype = "missing_available_memory"
            report.first_broken_edge = "available_memory_to_search_result"
        else:
            report.primary_subtype = "no_retrieval_problem"

        if report.problem_found and not report.review_required:
            report.recommended_route = LearningRoute.ACCESS_SKILL_MAKER
            memory_by_id = {
                str(item.get("version_id")): item
                for item in relevant_snapshot_memories
                if item.get("version_id")
            }
            report.repair_package = {
                "question": question,
                "missing_memories": [
                    memory_by_id[version_id]
                    for version_id in missing
                    if version_id in memory_by_id
                ],
                "conflicting_returned_version_ids": conflicting,
                "search_steps": report.search_steps,
                "reason": report.reason,
            }

        return report

    @staticmethod
    def _annotate_search_steps(
        search_steps: list[dict[str, Any]],
        necessary_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Show what each step returned and when each useful item first appeared."""
        seen: set[str] = set()
        annotated: list[dict[str, Any]] = []
        for raw_step in search_steps:
            step = dict(raw_step)
            returned = [
                str(item)
                for item in step.get("returned_version_ids", [])
                if item
            ]
            step["new_version_ids"] = [
                version_id for version_id in returned if version_id not in seen
            ]
            step["necessary_version_ids_returned"] = [
                version_id for version_id in returned
                if version_id in necessary_ids
            ]
            seen.update(returned)
            annotated.append(step)
        return annotated

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(
            str(item).strip() for item in value if str(item).strip()
        ))

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

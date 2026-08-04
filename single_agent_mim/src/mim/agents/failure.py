"""Shared answer-model check used beside the two independent diagnoses.

This is not a third failure classifier and never suppresses retrieval or
construction diagnosis. It only asks whether a stronger model can answer from
the exact memories returned to the runtime model.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..llm.base import ModelClient


BLIND_REANSWER_PROMPT = """\
Answer the question using only the memory records below. You do not know the
expected answer or the runtime model's prediction. Do not use outside facts.

Memory records:
{memories}

Question:
{question}

Return one direct answer. If the records are insufficient, return exactly:
No information available.
"""


ANSWER_JUDGE_PROMPT = """\
Judge whether a candidate answer is semantically correct and complete for the
question and reference answer. Harmless paraphrases are acceptable. Return:
{"correct": true, "reason": "brief reason"}
"""


class AnswerCheckAgent:
    """Re-answer from runtime-read memories, then judge semantic correctness."""

    def __init__(
        self,
        model: ModelClient,
        *,
        blind_reanswer_prompt: str = BLIND_REANSWER_PROMPT,
        answer_judge_prompt: str = ANSWER_JUDGE_PROMPT,
    ):
        self._model = model
        self._blind_prompt = blind_reanswer_prompt
        self._judge_prompt = answer_judge_prompt

    def check(
        self,
        *,
        question: str,
        reference_answer: str,
        returned_memories: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            maintenance_answer = self.reanswer(
                question=question,
                returned_memories=returned_memories,
            )
            judge_response = self._model.generate(
                [
                    {"role": "system", "content": self._judge_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "question": question,
                                "reference_answer": reference_answer,
                                "candidate_answer": maintenance_answer,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=500,
                json_mode=True,
            )
            judgment = self._parse_json(judge_response.text)
            if "correct" not in judgment:
                return {
                    "status": "model_error",
                    "maintenance_answer": maintenance_answer,
                    "correct": None,
                    "reason": "Answer judge returned invalid JSON.",
                    "input_version_ids": [
                        str(item.get("version_id"))
                        for item in returned_memories
                        if item.get("version_id")
                    ],
                }
            return {
                "status": "completed",
                "maintenance_answer": maintenance_answer,
                "correct": bool(judgment["correct"]),
                "reason": str(judgment.get("reason", "")).strip(),
                "input_version_ids": [
                    str(item.get("version_id"))
                    for item in returned_memories
                    if item.get("version_id")
                ],
            }
        except Exception as exc:
            return {
                "status": "model_error",
                "maintenance_answer": "",
                "correct": None,
                "reason": str(exc),
                "input_version_ids": [
                    str(item.get("version_id"))
                    for item in returned_memories
                    if item.get("version_id")
                ],
            }

    def reanswer(
        self,
        *,
        question: str,
        returned_memories: list[dict[str, Any]],
    ) -> str:
        prompt = (
            self._blind_prompt
            .replace("{question}", question)
            .replace("{memories}", self._format_memories(returned_memories))
            # Existing prompt files used this older placeholder.
            .replace(
                "{visible_memories}",
                self._format_memories(returned_memories),
            )
        )
        response = self._model.generate(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=700,
            json_mode=False,
        )
        return response.text.strip()

    @staticmethod
    def _format_memories(memories: list[dict[str, Any]]) -> str:
        if not memories:
            return "(No memory was returned.)"
        return "\n".join(
            (
                f"[{item.get('version_id', '?')}] "
                f"{item.get('memory_kind', 'memory')} | "
                f"subject={item.get('subject') or 'unknown'} | "
                f"world_start={item.get('world_start') or 'unknown'} | "
                f"world_end={item.get('world_end') or 'open'} | "
                f"{item.get('content', item.get('rendered_text', ''))}"
            )
            for item in memories
        )

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

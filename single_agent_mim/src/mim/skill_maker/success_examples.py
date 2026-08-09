"""Read-only index of successful Runtime Skill-use trajectories.

The index is deliberately separate from the official Skill Bank.  It gives the
candidate generator one observed, Judge-correct execution example for scope
calibration; it is never published or retrieved by Runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal


class SuccessfulSkillExampleIndex:
    """Select one deterministic successful trajectory for a diagnosis."""

    def __init__(self, examples: list[dict[str, Any]]):
        self._by_side: dict[str, list[dict[str, Any]]] = {
            "access": [],
            "construction": [],
        }
        for example in examples:
            side = str(example.get("side", ""))
            if side not in self._by_side:
                continue
            if example.get("judge_label") != "C":
                continue
            skill_ids = [
                str(value)
                for value in example.get("skill_ids", [])
                if str(value)
            ]
            if not skill_ids:
                continue
            normalized = dict(example)
            normalized["skill_ids"] = list(dict.fromkeys(skill_ids))
            self._by_side[side].append(normalized)

        for side in self._by_side:
            self._by_side[side].sort(
                key=lambda item: (
                    str(item.get("conversation_id", "")),
                    str(item.get("qa_id", "")),
                    str(item.get("session_id", "")),
                    ",".join(item.get("skill_ids", [])),
                )
            )

    @classmethod
    def load(cls, path: str | Path) -> "SuccessfulSkillExampleIndex":
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(
                f"Successful Skill trajectory index not found: {source}"
            )
        if source.suffix.lower() == ".jsonl":
            rows = []
            for line_no, line in enumerate(
                source.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(
                        f"Expected JSON object at {source}:{line_no}"
                    )
                rows.append(value)
        else:
            value = json.loads(source.read_text(encoding="utf-8"))
            rows = value.get("examples", []) if isinstance(value, dict) else value
            if not isinstance(rows, list):
                raise ValueError(
                    "Successful Skill trajectory file must contain a JSON list "
                    "or an object with an examples list."
                )
        return cls(rows)

    def count(self, side: Literal["access", "construction"] | None = None) -> int:
        if side is not None:
            return len(self._by_side[side])
        return sum(len(items) for items in self._by_side.values())

    def select(
        self,
        *,
        side: Literal["access", "construction"],
        official_skill_trace: Any,
    ) -> dict[str, Any] | None:
        """Prefer an exact selected-Skill match, then nearby, then a fallback.

        A same-side fallback is useful when the failed trace contains no Skill
        with a prior correct use.  It still calibrates the expected abstraction
        and trajectory format, and is explicitly labelled as non-matching.
        """
        examples = self._by_side[side]
        if not examples:
            return None

        selected_ids, nearby_ids = _trace_skill_ids(official_skill_trace)
        for relation, wanted in (
            ("same_selected_skill", selected_ids),
            ("same_nearby_skill", nearby_ids),
        ):
            for example in examples:
                if wanted.intersection(example.get("skill_ids", [])):
                    return {
                        "relationship_to_diagnosis": relation,
                        **example,
                    }
        return {
            "relationship_to_diagnosis": "same_side_calibration_only",
            **examples[0],
        }


class NoSkillSuccessIndex:
    """Judge-correct questions answered by the DEFAULT policy (no Skill).

    These are the positive examples the candidate generator must not break:
    a question pattern answered correctly without any Skill is evidence that
    the default behaviour is sufficient for that pattern, so proposed Skills
    must be conditioned on the default policy having failed first.
    """

    def __init__(self, examples: list[dict[str, Any]]):
        self._examples: list[dict[str, Any]] = []
        for example in examples:
            if str(example.get("judge_label", "")).upper() != "C":
                continue
            if example.get("skill_ids"):
                # Skill usage means this is not a default-policy example.
                continue
            question = str(example.get("question", "")).strip()
            if not question:
                continue
            self._examples.append(dict(example))
        self._examples.sort(
            key=lambda item: (
                str(item.get("conversation_id", "")),
                str(item.get("qa_id", "")),
            )
        )

    @classmethod
    def load(cls, path: str | Path) -> "NoSkillSuccessIndex":
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(
                f"Default-policy success index not found: {source}"
            )
        rows = []
        for line_no, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(
                    f"Expected JSON object at {source}:{line_no}"
                )
            rows.append(value)
        return cls(rows)

    def count(self) -> int:
        return len(self._examples)

    def select(self, diagnosis: dict[str, Any]) -> dict[str, Any] | None:
        """Return the most similar default-policy success, or None."""
        question = str(diagnosis.get("question", "")).strip()
        if not question or not self._examples:
            return None
        query_tokens = _tokenize(question)
        if not query_tokens:
            return None
        best: dict[str, Any] | None = None
        best_score = 0.0
        for example in self._examples:
            tokens = _tokenize(str(example.get("question", "")))
            overlap = len(query_tokens & tokens)
            if not overlap:
                continue
            score = overlap / max(len(query_tokens | tokens), 1)
            if score > best_score:
                best_score = score
                best = example
        if best is None or best_score < 0.15:
            return None
        return {
            "relationship_to_diagnosis": "default_policy_success",
            "similarity": round(best_score, 3),
            "qa_id": best.get("qa_id"),
            "conversation_id": best.get("conversation_id"),
            "question": best.get("question"),
            "category": best.get("category"),
            "prediction": best.get("prediction"),
            "reference_answer": best.get("reference_answer"),
        }


def _tokenize(text: str) -> set[str]:
    import re

    return set(re.findall(r"[a-z0-9]+", str(text).lower()))


def _trace_skill_ids(trace: Any) -> tuple[set[str], set[str]]:
    selected: set[str] = set()
    nearby: set[str] = set()
    traces = trace if isinstance(trace, list) else [trace]
    for item in traces:
        if not isinstance(item, dict):
            continue
        selected.update(
            str(skill.get("skill_id"))
            for skill in item.get("selected", [])
            if isinstance(skill, dict) and skill.get("skill_id")
        )
        nearby.update(
            str(skill.get("skill_id"))
            for skill in item.get("nearby_not_selected", [])
            if isinstance(skill, dict) and skill.get("skill_id")
        )
    return selected, nearby

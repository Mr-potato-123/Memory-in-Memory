"""Small, shared helpers for strict JSON-only diagnosis calls."""

from __future__ import annotations

import json
import re
from typing import Any

from ..llm.base import ModelClient


class InvalidModelOutput(ValueError):
    """Raised when a diagnosis response violates its JSON contract."""


def call_json(
    model: ModelClient,
    *,
    prompt: str,
    payload: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any]:
    # DeepSeek thinking tokens share the completion budget. Diagnosis calls
    # must leave room for the final JSON after the private reasoning trace.
    effective_max_tokens = max(max_tokens, 16000)
    response = model.generate(
        [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        # Diagnosis is deliberately sampled with thinking enabled. This is
        # analysis-only; runtime baseline generation remains unchanged.
        temperature=0.2,
        max_tokens=effective_max_tokens,
        json_mode=True,
    )
    parsed = parse_json_object(response.text)
    if not parsed:
        raise InvalidModelOutput("The model did not return a JSON object.")
    return parsed


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


def unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            str(item).strip()
            for item in value
            if str(item).strip()
        )
    )


def confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def require_known_ids(ids: list[str], allowed: set[str], field: str) -> None:
    unknown = sorted(set(ids) - allowed)
    if unknown:
        raise InvalidModelOutput(
            f"{field} contains IDs that were not supplied: {unknown}"
        )

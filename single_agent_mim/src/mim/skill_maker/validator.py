"""Skill Payload Validator — deterministic pre-replay checks.

Ensures Skills don't contain PII, IDs, reference answers, or invalid instructions.
No LLM involved.
"""

from __future__ import annotations

import re

from .models import SkillPayload


class SkillPayloadValidator:
    """Validate a Skill payload before any replay is attempted."""

    def __init__(
        self,
        name_max_chars: int = 60,
        description_max_chars: int = 200,
        content_max_chars: int = 600,
        content_max_items: int = 3,
        content_item_max_chars: int = 200,
    ):
        self._name_max = name_max_chars
        self._desc_max = description_max_chars
        self._content_max = content_max_chars
        self._content_max_items = max(1, content_max_items)
        self._content_item_max = content_item_max_chars

        # Patterns that must NOT appear in any Skill
        self._id_pattern = re.compile(
            r'(mem_\w+_v\d+|conv\d+_s\d+_m\d+|msg_\w+|'
            r'cand_\w+|failure_\w+|skill_\w+|'
            r'sk_\w+_v\d+)',
            re.IGNORECASE,
        )

    def validate(
        self,
        payload: SkillPayload,
        side: str | None = None,
        reference_answer: str = "",
        question_entities: list[str] | None = None,
        gold_message_ids: list[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Validate a payload. Returns (is_valid, error_messages)."""
        errors: list[str] = []

        # Non-empty
        if not payload.name.strip():
            errors.append("name is empty")
        if not payload.description.strip():
            errors.append("description is empty")
        if not payload.content:
            errors.append("content is empty")
        content_text = payload.content_text()

        # Length limits
        if len(payload.name) > self._name_max:
            errors.append(f"name too long: {len(payload.name)} > {self._name_max}")
        if len(payload.description) > self._desc_max:
            errors.append(f"description too long: {len(payload.description)} > {self._desc_max}")
        if len(content_text) > self._content_max:
            errors.append(
                f"content too long: {len(content_text)} > {self._content_max}"
            )
        if len(payload.content) > self._content_max_items:
            errors.append(
                f"content too many items: {len(payload.content)} > "
                f"{self._content_max_items}"
            )
        for index, item in enumerate(payload.content):
            if len(item) > self._content_item_max:
                errors.append(
                    f"content item {index} too long: {len(item)} > "
                    f"{self._content_item_max}"
                )

        # No system IDs
        combined = f"{payload.name} {payload.description} {content_text}"
        if self._id_pattern.search(combined):
            errors.append("contains system IDs (memory IDs, message IDs, etc.)")

        # No instruction to emit the reference answer.  A plain substring
        # check is too aggressive: a generic Construction rule may naturally
        # mention the same activity, attribute, or place as one training
        # answer.  Treat it as leakage only when a field is the answer itself
        # or imperative wording tells the Runtime to return it.
        if reference_answer and reference_answer.strip():
            ref_norm = reference_answer.strip().lower()
            field_values = [
                payload.name,
                payload.description,
                *payload.content,
            ]
            direct_match = any(
                re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", value.lower()).strip()
                == re.sub(
                    r"[^a-z0-9\u3400-\u9fff]+",
                    " ",
                    ref_norm,
                ).strip()
                for value in field_values
            )
            imperative_leak = bool(
                len(ref_norm) > 3
                and re.search(
                    r"\b(?:return|answer|output|respond(?: with)?)\b"
                    r"[^.\n]{0,60}"
                    + re.escape(ref_norm),
                    combined.lower(),
                )
            )
            if direct_match or imperative_leak:
                errors.append("contains reference answer")

        # No question entities
        if question_entities:
            for ent in question_entities:
                if len(ent) > 3 and ent.lower() in combined.lower():
                    errors.append(f"contains question entity: {ent}")
                    break

        # No gold message IDs
        if gold_message_ids:
            for mid in gold_message_ids:
                if mid in combined:
                    errors.append(f"contains gold message ID: {mid}")
                    break

        # Content must be actionable strategy, not case narrative
        narrative_markers = ["the question asked", "the user said",
                             "the answer should have been", "this specific case"]
        for marker in narrative_markers:
            if marker.lower() in content_text.lower():
                errors.append(f"content reads like case narrative: '{marker}'")

        # Description must describe trigger conditions
        if not any(word in payload.description.lower()
                   for word in ["when", "if", "use", "apply", "before", "during"]):
            errors.append("description lacks trigger condition wording")

        # Side contracts are deterministic and must survive candidate
        # generation and publication. Construction is extraction-only; the
        # program owns append/dedup behavior.
        normalized_side = (side or "").lower()
        if normalized_side == "construction":
            unsupported_kind = re.search(
                r"(?:memory_kind|memory kind|memories? of type)\s*"
                r"[=:]?\s*['\"]?(metric|attribute|fact|intention)\b",
                combined,
                re.IGNORECASE,
            )
            if unsupported_kind:
                errors.append(
                    "construction Skill references unsupported memory kind: "
                    f"{unsupported_kind.group(1)}"
                )
            if re.search(
                r"\b(all plausible (?:locations|options)|"
                r"automatically determine (?:its |the )?(?:typical )?"
                r"geographic location)\b",
                combined,
                re.IGNORECASE,
            ):
                errors.append(
                    "construction Skill permits unsupported factual inference"
                )
            if re.search(
                r"\b(?:update|merge|delete|overwrite|replace|retract|"
                r"supersede|target)\b[^.\n]{0,80}\b(?:memory|memories|"
                r"memory_id|version|record|database)\b|"
                r"\b(?:memory|memories|memory_id|version|record|database)\b"
                r"[^.\n]{0,80}\b(?:update|merge|delete|overwrite|replace|"
                r"retract|supersede|target)\b",
                combined,
                re.IGNORECASE,
            ):
                errors.append(
                    "construction Skill requests forbidden storage mutation"
                )

        if normalized_side == "access" and re.search(
            r"\b(return|answer)\s+(?:with\s+)?the\s+(?:known|expected|"
            r"reference|gold)\s+answer\b",
            combined,
            re.IGNORECASE,
        ):
            errors.append("access Skill bypasses evidence-grounded retrieval")

        return len(errors) == 0, errors

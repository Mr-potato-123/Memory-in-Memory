"""Mock client — returns fixture outputs for testing without API keys.

Supports:
  - A default set of canned responses keyed by agent role.
  - Injection of custom response sequences (for replay scenarios).
  - Simulation of malformed JSON and protocol errors.
"""

from __future__ import annotations

import json
from typing import Optional

from ..config import ModelConfig
from ..schemas import ModelResponse


class MockClient:
    """Returns pre-configured responses for deterministic testing."""

    def __init__(self, cfg: ModelConfig):
        self._model = cfg.model
        # Allow callers to inject a sequence of responses.
        self._script: list[ModelResponse] = []
        self._idx: int = 0
        # Default canned responses keyed by a simple role tag.
        self._defaults: dict[str, ModelResponse] = {}

    # ── Injection API ──────────────────────────────────────────

    def set_script(self, responses: list[ModelResponse]):
        """Set a fixed sequence of responses to replay in order."""
        self._script = list(responses)
        self._idx = 0

    def set_default(self, key: str, text_or_response: str | ModelResponse):
        """Register a default response for a given agent key."""
        if isinstance(text_or_response, str):
            self._defaults[key] = ModelResponse(
                text=text_or_response,
                provider="mock",
                model=self._model,
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=5,
                finish_reason="stop",
            )
        else:
            self._defaults[key] = text_or_response

    def _make_resp(self, text: str) -> ModelResponse:
        return ModelResponse(
            text=text,
            provider="mock",
            model=self._model,
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=5,
            finish_reason="stop",
        )

    # ── Default canned responses ───────────────────────────────

    @staticmethod
    def construction_add_memory(content: str = "Alice lives in Seattle.", event_time: str = "2023-05") -> str:
        return json.dumps({
            "action": "add_memory",
            "arguments": {
                "content": content,
                "event_time": event_time,
                "source_message_ids": ["msg_1"],
            },
            "reason": "New durable fact detected.",
        })

    @staticmethod
    def construction_finish() -> str:
        return json.dumps({
            "action": "finish",
            "arguments": {},
            "reason": "All facts processed.",
        })

    @staticmethod
    def access_answer(answer: str, evidence_ids: list[str] | None = None) -> str:
        return json.dumps({
            "action": "answer",
            "arguments": {
                "answer": answer,
                "evidence_ids": evidence_ids or ["mem_0001_v1"],
            },
            "reason": "Evidence sufficient.",
        })

    @staticmethod
    def access_search(method: str = "semantic", query: str = "test") -> str:
        return json.dumps({
            "action": "search_memory",
            "arguments": {"method": method, "query": query, "top_k": 5},
            "reason": "Looking for relevant facts.",
        })

    @staticmethod
    def failure_attribution(label: str = "construction") -> str:
        return json.dumps({
            "label": label,
            "confidence": 0.9,
            "reason": "Mock attribution.",
            "source_evidence_ids": ["msg_1"],
            "memory_evidence_ids": [],
            "access_evidence_ids": [],
            "failure_signature": "missing_fact",
        })

    @staticmethod
    def skill_draft(side: str = "access", name: str = "Mock Skill") -> str:
        return json.dumps({
            "side": side,
            "name": name,
            "description": f"A mock skill for {side}.",
            "content": f"When doing {side}, always check the timestamp first.",
        })

    @staticmethod
    def skill_integrate(action: str = "create") -> str:
        return json.dumps({
            "action": action,
            "reason": "This is a new pattern, no existing skill covers it.",
        })

    @staticmethod
    def malformed_json() -> str:
        return "not valid json at all {{{"

    # ── Protocol ───────────────────────────────────────────────

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1200,
        json_mode: bool = False,
    ) -> ModelResponse:
        # 1) Script takes priority
        if self._idx < len(self._script):
            resp = self._script[self._idx]
            self._idx += 1
            return resp

        # 2) Try to match a default by scanning messages for a known key
        for m in messages:
            content = m.get("content", "")
            for key in self._defaults:
                if key in content:
                    return self._defaults[key]

        # 3) Generic fallback
        return self._make_resp(json.dumps({"action": "finish", "arguments": {}, "reason": "mock fallback"}))

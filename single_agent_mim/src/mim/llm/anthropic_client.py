"""Anthropic client — supports Claude via the Anthropic SDK.

Adapts Anthropic's system-message and usage format to the unified ModelClient protocol.
"""

from __future__ import annotations

import os
import time

from ..config import ModelConfig
from ..schemas import ModelResponse
from .base import LLMError, RateLimitError, TimeoutError as LLMTimeoutError


class AnthropicClient:
    """Thin wrapper around the anthropic SDK for Claude messages."""

    def __init__(self, cfg: ModelConfig):
        self._cfg = cfg
        self._model = cfg.model
        self._temperature = cfg.temperature
        self._max_tokens = cfg.max_tokens
        self._timeout = cfg.timeout_seconds
        self._max_retries = cfg.max_retries

        import anthropic
        api_key = os.environ.get(cfg.api_key_env or "MAINTENANCE_API_KEY", "sk-placeholder")
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=cfg.timeout_seconds,
            max_retries=0,
        )

    @staticmethod
    def _split_system_messages(messages: list[dict[str, str]]) -> tuple[str | None, list[dict[str, str]]]:
        """Extract system message(s) from the messages list.

        Anthropic API expects a top-level `system` param, not a message with role="system".
        """
        system_parts: list[str] = []
        rest: list[dict[str, str]] = []
        for m in messages:
            if m["role"] == "system":
                system_parts.append(m["content"])
            else:
                rest.append(m)
        system = "\n\n".join(system_parts) if system_parts else None
        return system, rest

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ModelResponse:
        temp = temperature if temperature is not None else self._temperature
        mt = max_tokens if max_tokens is not None else self._max_tokens

        system, user_messages = self._split_system_messages(messages)

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                t0 = time.time()

                # Build kwargs
                kwargs: dict = dict(
                    model=self._model,
                    messages=user_messages,
                    max_tokens=mt,
                    temperature=temp,
                )
                if system:
                    kwargs["system"] = system

                resp = self._client.messages.create(**kwargs)
                elapsed_ms = int((time.time() - t0) * 1000)

                # Extract text from the first content block
                text = ""
                for block in resp.content:
                    if hasattr(block, "text"):
                        text += block.text

                return ModelResponse(
                    text=text,
                    provider="anthropic",
                    model=self._model,
                    prompt_tokens=resp.usage.input_tokens if resp.usage else None,
                    completion_tokens=resp.usage.output_tokens if resp.usage else None,
                    latency_ms=elapsed_ms,
                    finish_reason=resp.stop_reason,
                )
            except Exception as exc:
                last_error = exc
                msg = str(exc).lower()
                if "rate" in msg or "429" in str(exc):
                    if attempt < self._max_retries:
                        time.sleep(min(2 ** attempt, 30))
                        continue
                    raise RateLimitError(str(exc)) from exc
                if "timeout" in msg:
                    if attempt < self._max_retries:
                        continue
                    raise LLMTimeoutError(str(exc)) from exc
                if attempt < self._max_retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise LLMError(str(exc)) from exc

        raise LLMError(str(last_error))

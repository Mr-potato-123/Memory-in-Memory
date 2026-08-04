"""OpenAI-compatible client — supports GPT, Qwen, and any OpenAI-API endpoint."""

from __future__ import annotations

import os
import time

from ..config import ModelConfig
from ..schemas import ModelResponse
from .base import (
    LLMError,
    ProtocolError,
    RateLimitError,
    TimeoutError as LLMTimeoutError,
)


class OpenAICompatibleClient:
    """Thin wrapper around the openai SDK for chat completions."""

    def __init__(self, cfg: ModelConfig):
        self._cfg = cfg
        self._model = cfg.model
        self._temperature = cfg.temperature
        self._max_tokens = cfg.max_tokens
        self._timeout = cfg.timeout_seconds
        self._max_retries = cfg.max_retries

        import openai
        api_key = cfg.api_key or os.environ.get(
            cfg.api_key_env or "RUNTIME_API_KEY",
            "sk-placeholder",
        )
        base_url = cfg.base_url or None
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=cfg.timeout_seconds,
            max_retries=0,  # we handle retries ourselves
        )

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

        kwargs: dict = dict(
            model=self._model,
            messages=messages,
            temperature=temp,
            max_tokens=mt,
        )
        if json_mode and self._cfg.supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self._cfg.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self._cfg.reasoning_effort
        if self._cfg.extra_body:
            kwargs["extra_body"] = self._cfg.extra_body

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                t0 = time.time()
                resp = self._client.chat.completions.create(**kwargs)
                elapsed_ms = int((time.time() - t0) * 1000)

                choice = resp.choices[0]
                text = choice.message.content or ""
                message_extra = getattr(choice.message, "model_extra", None) or {}
                reasoning = (
                    getattr(choice.message, "reasoning_content", None)
                    or getattr(choice.message, "thinking", None)
                    or message_extra.get("reasoning_content")
                    or message_extra.get("thinking")
                    or message_extra.get("reasoning")
                )
                if self._cfg.reject_reasoning_output and (
                    reasoning or "<think" in text.lower() or "</think>" in text.lower()
                ):
                    raise ProtocolError(
                        "Non-thinking contract violated: response contained reasoning output"
                    )
                return ModelResponse(
                    text=text,
                    provider="openai_compatible",
                    model=self._model,
                    prompt_tokens=resp.usage.prompt_tokens if resp.usage else None,
                    completion_tokens=resp.usage.completion_tokens if resp.usage else None,
                    latency_ms=elapsed_ms,
                    finish_reason=choice.finish_reason,
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

"""ModelClient protocol and common exceptions."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schemas import ModelResponse


class LLMError(Exception):
    """Base exception for LLM calls."""


class RateLimitError(LLMError):
    """Rate-limited by the provider."""


class ProtocolError(LLMError):
    """Model output could not be parsed as valid JSON instructions."""


class TimeoutError(LLMError):
    """Model call timed out."""


@runtime_checkable
class ModelClient(Protocol):
    """Unified protocol for all LLM providers.

    Every provider (OpenAI-compatible, Anthropic, Mock) must satisfy
    this interface so that Agents never branch on provider identity.
    """

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1200,
        json_mode: bool = False,
    ) -> ModelResponse:
        """Send messages to the model and return a unified response."""
        ...

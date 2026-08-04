"""Thread-safe round-robin routing across equivalent model clients."""

from __future__ import annotations

import threading

from ..schemas import ModelResponse
from .base import LLMError, ModelClient


class RoundRobinModelClient:
    """Distribute independent calls across a local API-key pool.

    Clients must target the same provider, model, and generation settings.
    A failed key is followed by each remaining key once before the call fails.
    API keys are never included in responses, exceptions, or artifacts.
    """

    def __init__(self, clients: list[ModelClient]):
        if not clients:
            raise ValueError("RoundRobinModelClient requires at least one client.")
        self._clients = list(clients)
        self._lock = threading.Lock()
        self._next = 0

    def _start_index(self) -> int:
        with self._lock:
            index = self._next
            self._next = (self._next + 1) % len(self._clients)
            return index

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ModelResponse:
        start = self._start_index()
        last_error: Exception | None = None
        for offset in range(len(self._clients)):
            client = self._clients[(start + offset) % len(self._clients)]
            try:
                return client.generate(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except LLMError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

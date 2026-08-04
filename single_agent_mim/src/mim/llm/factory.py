"""ModelClient factory — creates the right client from config."""

from __future__ import annotations

from ..config import ModelConfig
from .base import ModelClient
from .mock_client import MockClient
from .openai_compatible import OpenAICompatibleClient
from .round_robin import RoundRobinModelClient
from .anthropic_client import AnthropicClient


def create_client(cfg: ModelConfig) -> ModelClient:
    """Instantiate a ModelClient based on config.

    Returns the appropriate implementation for:
      - openai_compatible  (GPT, Qwen, and other OpenAI-API-compatible providers)
      - anthropic          (Claude via Anthropic SDK)
      - mock               (returns fixture outputs, no API key needed)
    """
    provider = cfg.provider.lower()

    if cfg.api_keys:
        if provider != "openai_compatible":
            raise ValueError("API-key pools require an OpenAI-compatible provider.")
        clients = []
        for api_key in cfg.api_keys:
            child = cfg.model_copy(
                deep=True,
                update={"api_key": api_key, "api_keys": []},
            )
            clients.append(OpenAICompatibleClient(child))
        return RoundRobinModelClient(clients)

    if provider == "openai_compatible":
        return OpenAICompatibleClient(cfg)
    elif provider == "anthropic":
        return AnthropicClient(cfg)
    elif provider == "mock":
        return MockClient(cfg)
    else:
        raise ValueError(
            f"Unknown model provider: {cfg.provider}. "
            f"Expected one of: openai_compatible, anthropic, mock."
        )

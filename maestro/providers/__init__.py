"""Provider registry. Resolves a model name to a concrete async chat client."""

from __future__ import annotations

from ..config import Config, get_config, get_security
from .base import ChatResult, Provider, ProviderError, RateLimitedError
from .gemini_provider import GeminiProvider
from .groq_provider import GroqProvider
from .mock_provider import MockProvider

__all__ = [
    "ChatResult",
    "Provider",
    "ProviderError",
    "RateLimitedError",
    "build_registry",
    "ProviderRegistry",
]


class ProviderRegistry:
    def __init__(self, providers: dict[str, Provider]):
        self._providers = providers

    def for_model(self, provider_name: str) -> Provider:
        if provider_name not in self._providers:
            raise ProviderError(f"No provider configured for '{provider_name}'")
        return self._providers[provider_name]


def build_registry(config: Config | None = None) -> ProviderRegistry:
    config = config or get_config()
    sec = get_security()
    providers: dict[str, Provider] = {}

    for name, conf in config.providers.items():
        key = config.api_key_for(name)
        if key:
            if name == "groq":
                providers[name] = GroqProvider(conf, key)
            elif name == "gemini":
                providers[name] = GeminiProvider(conf, key)
        elif sec.allow_mock:
            # No key for this provider -> deterministic mock so demos/CI still run.
            providers[name] = MockProvider(name)
        # else: leave this provider unregistered. ResilientCaller's fallback chain
        # skips it (registry.for_model raises -> caller tries the next model), so a
        # single missing key never crashes startup — it just narrows the model pool.

    return ProviderRegistry(providers)

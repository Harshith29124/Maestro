"""Provider interface + shared result/error types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class ChatResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    raw: dict = field(default_factory=dict)


class ProviderError(Exception):
    """Non-retryable provider failure (bad request, auth, etc.)."""


class RateLimitedError(ProviderError):
    """HTTP 429. Carries retry-after seconds when the provider supplies it."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class Provider(Protocol):
    name: str

    async def chat(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        timeout_s: float = 60,
    ) -> ChatResult:
        ...

    async def aclose(self) -> None:
        ...


def estimate_tokens(text: str) -> int:
    """Cheap heuristic (~4 chars/token) used for TPM accounting before a call.

    Deliberately conservative-leaning so we under-spend the budget rather than 429.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)

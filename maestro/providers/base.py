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


import re as _re

# Reasoning models (gpt-oss, qwen3.x) leak chain-of-thought as <think>...</think>
# (and a few sibling tags). Strip it so it never reaches the user or the next role.
_THINK_RE = _re.compile(
    r"<(think|thinking|reasoning|thought|scratchpad)>.*?</\1>",
    _re.IGNORECASE | _re.DOTALL,
)
# An unclosed leading <think> (truncated output) — drop everything up to the close.
_OPEN_THINK_RE = _re.compile(
    r"^\s*<(think|thinking|reasoning|thought|scratchpad)>.*?(</\1>|$)",
    _re.IGNORECASE | _re.DOTALL,
)


def clean_output(text: str) -> str:
    """Remove leaked reasoning blocks and tidy whitespace for user-facing text."""
    if not text:
        return ""
    cleaned = _THINK_RE.sub("", text)
    cleaned = _OPEN_THINK_RE.sub("", cleaned)
    # Drop stray scaffolding labels a model might echo from the prompt.
    cleaned = _re.sub(
        r"^\s*(STRATEGY( USED)?|VERIFIED ANSWER|TASK)\s*:\s*",
        "",
        cleaned,
        flags=_re.IGNORECASE | _re.MULTILINE,
    )
    return cleaned.strip()

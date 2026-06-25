"""Provider-side rate limiting + resilient call execution.

Two layers live here:

1. `TokenBucketLimiter` — per-model RPM *and* TPM enforcement over a rolling 60s window.
   TPM is the binding constraint on Groq's free tier (8K TPM), so we reserve an estimated
   token cost *before* a call and defer if it would blow the budget.

2. `ResilientCaller` — wraps a provider call with exponential backoff + jitter on 429
   (honoring the provider's `retry-after`), per-call timeout, and a model fallback chain.

This is provider-side throttling — distinct from the per-client HTTP rate limiting in the
API layer (`api/security.py`), which protects the deployment from abusive callers.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

from .config import Config, ModelSpec
from .providers import ChatResult, ProviderRegistry, RateLimitedError
from .providers.base import estimate_tokens


@dataclass
class _Window:
    """Sliding 60s ledger of (timestamp, tokens) for one model."""

    events: list[tuple[float, int]] = field(default_factory=list)

    def prune(self, now: float) -> None:
        cutoff = now - 60.0
        self.events = [(t, tok) for (t, tok) in self.events if t >= cutoff]

    def requests(self) -> int:
        return len(self.events)

    def tokens(self) -> int:
        return sum(tok for _, tok in self.events)


class TokenBucketLimiter:
    """Enforces RPM and TPM per model before requests leave the process."""

    def __init__(self, config: Config):
        self._config = config
        self._windows: dict[str, _Window] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _win(self, model: str) -> _Window:
        return self._windows.setdefault(model, _Window())

    def _lock(self, model: str) -> asyncio.Lock:
        return self._locks.setdefault(model, asyncio.Lock())

    async def acquire(self, spec: ModelSpec, est_tokens: int) -> None:
        """Block until the model has RPM+TPM headroom for `est_tokens`, then reserve it."""
        async with self._lock(spec.name):
            while True:
                now = time.monotonic()
                win = self._win(spec.name)
                win.prune(now)

                rpm_ok = win.requests() < spec.rpm
                tpm_ok = win.tokens() + est_tokens <= spec.tpm
                if rpm_ok and tpm_ok:
                    win.events.append((now, est_tokens))
                    return

                # Sleep until the oldest event ages out of the window.
                wait = max(0.05, 60.0 - (now - win.events[0][0])) if win.events else 0.05
                await asyncio.sleep(min(wait, 5.0))

    def reconcile(self, model: str, actual_tokens: int, est_tokens: int) -> None:
        """Replace the reserved estimate with the real usage once known."""
        win = self._win(model)
        if win.events:
            ts, _ = win.events[-1]
            win.events[-1] = (ts, actual_tokens if actual_tokens else est_tokens)

    def snapshot(self) -> dict[str, dict[str, int]]:
        now = time.monotonic()
        out: dict[str, dict[str, int]] = {}
        for model, win in self._windows.items():
            win.prune(now)
            out[model] = {"rpm_used": win.requests(), "tpm_used": win.tokens()}
        return out


@dataclass
class CallOutcome:
    result: ChatResult
    model_used: str
    fallbacks: list[str]
    latency_ms: int


class ResilientCaller:
    """Executes a role call across a fallback chain with backoff on 429."""

    def __init__(
        self,
        config: Config,
        registry: ProviderRegistry,
        limiter: TokenBucketLimiter,
        max_attempts_per_model: int = 3,
    ):
        self._config = config
        self._registry = registry
        self._limiter = limiter
        self._max_attempts = max_attempts_per_model

    async def call_role(
        self,
        role: str,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CallOutcome:
        chain = self._config.models_for_role(role)
        if not chain:
            raise ValueError(f"No models configured for role '{role}'")
        return await self._call_chain(
            chain, role, system=system, user=user,
            temperature=temperature, max_tokens=max_tokens,
        )

    async def call_models(
        self,
        model_names: list[str],
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CallOutcome:
        """Call a specific ordered list of models (by name), first available wins.

        Used by Consensus mode to pin each proposer to a distinct model family.
        Unknown names are skipped; remaining models act as the fallback chain.
        """
        chain = [self._config.models[n] for n in model_names if n in self._config.models]
        if not chain:
            raise ValueError(f"No known models in {model_names}")
        return await self._call_chain(
            chain, "/".join(model_names), system=system, user=user,
            temperature=temperature, max_tokens=max_tokens,
        )

    async def _call_chain(
        self,
        chain: list[ModelSpec],
        label: str,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CallOutcome:
        temperature = (
            temperature if temperature is not None else self._config.default_temperature
        )
        max_tokens = max_tokens or self._config.default_max_tokens
        est = estimate_tokens(system) + estimate_tokens(user) + max_tokens

        fallbacks: list[str] = []
        last_err: Exception | None = None
        start = time.monotonic()

        for spec in chain:
            provider = self._registry.for_model(spec.provider)
            for attempt in range(self._max_attempts):
                try:
                    await self._limiter.acquire(spec, est)
                    result = await provider.chat(
                        model_id=spec.api_id,
                        system=system,
                        user=user,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout_s=self._config.per_call_timeout_s,
                    )
                    self._limiter.reconcile(
                        spec.name, result.tokens_in + result.tokens_out, est
                    )
                    return CallOutcome(
                        result=result,
                        model_used=spec.name,
                        fallbacks=fallbacks,
                        latency_ms=int((time.monotonic() - start) * 1000),
                    )
                except RateLimitedError as exc:
                    last_err = exc
                    delay = self._backoff(attempt, exc.retry_after)
                    fallbacks.append(f"{spec.name}:429:retry_in_{delay:.1f}s")
                    if attempt < self._max_attempts - 1:
                        await asyncio.sleep(delay)
                    # else: fall through to next model in the chain
                except Exception as exc:  # noqa: BLE001 - continue-on-fail semantics
                    last_err = exc
                    fallbacks.append(f"{spec.name}:error:{type(exc).__name__}")
                    break  # don't retry hard errors; try next model

        raise RuntimeError(
            f"All models for '{label}' failed. Last error: {last_err}"
        )

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after + random.uniform(0, 0.5)
        base = min(16.0, 2.0 ** attempt)
        return base + random.uniform(0, base * 0.25)

"""Thinker — produces a strategy/outline to guide the Worker."""

from __future__ import annotations

from ..prompts import THINKER
from ..ratelimit import ResilientCaller
from ..schemas import Step, TokenUsage


async def run_thinker(caller: ResilientCaller, task: str) -> Step:
    outcome = await caller.call_role("thinker", system=THINKER, user=task)
    return Step(
        step="think",
        role="thinker",
        model=outcome.model_used,
        routing_rationale="Strongest free reasoning model plans the approach.",
        output=outcome.result.text,
        tokens=TokenUsage(
            **{"in": outcome.result.tokens_in, "out": outcome.result.tokens_out}
        ),
        latency_ms=outcome.latency_ms,
        error="; ".join(outcome.fallbacks) or None,
    )

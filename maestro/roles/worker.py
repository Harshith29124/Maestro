"""Worker — executes the task using the Thinker's strategy."""

from __future__ import annotations

from ..prompts import WORKER
from ..ratelimit import ResilientCaller
from ..schemas import Step, TokenUsage


async def run_worker(
    caller: ResilientCaller,
    task: str,
    strategy: str,
    correction: str | None = None,
) -> Step:
    user = f"TASK:\n{task}\n\nSTRATEGY:\n{strategy}"
    if correction:
        user += f"\n\nThe previous attempt was rejected. Fix these issues:\n{correction}"
    outcome = await caller.call_role("worker", system=WORKER, user=user)
    return Step(
        step="work",
        role="worker",
        model=outcome.model_used,
        routing_rationale="Fastest free worker executes the plan.",
        output=outcome.result.text,
        retry_triggered=correction is not None,
        tokens=TokenUsage(
            **{"in": outcome.result.tokens_in, "out": outcome.result.tokens_out}
        ),
        latency_ms=outcome.latency_ms,
        error="; ".join(outcome.fallbacks) or None,
    )

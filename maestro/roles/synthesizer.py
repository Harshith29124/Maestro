"""Synthesizer — merges verified pieces into the final user-facing answer."""

from __future__ import annotations

from ..prompts import SYNTHESIZER
from ..ratelimit import ResilientCaller
from ..schemas import Step, TokenUsage


async def run_synthesizer(
    caller: ResilientCaller, task: str, strategy: str, answer: str
) -> Step:
    user = (
        f"TASK:\n{task}\n\nSTRATEGY USED:\n{strategy}\n\n"
        f"VERIFIED ANSWER:\n{answer}\n\n"
        "Deliver the final, polished answer to the user."
    )
    outcome = await caller.call_role("synthesizer", system=SYNTHESIZER, user=user)
    return Step(
        step="synthesize",
        role="synthesizer",
        model=outcome.model_used,
        routing_rationale="Large-context model merges verified pieces into the final answer.",
        output=outcome.result.text,
        tokens=TokenUsage(
            **{"in": outcome.result.tokens_in, "out": outcome.result.tokens_out}
        ),
        latency_ms=outcome.latency_ms,
        error="; ".join(outcome.fallbacks) or None,
    )

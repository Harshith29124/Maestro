"""Conductor — reads the task and emits a routing plan with a rationale.

Prompt/rule-based, NOT a trained coordinator like Fugu's. Disclosed honestly.
Includes a deterministic fallback plan so a malformed conductor response never
crashes a run.
"""

from __future__ import annotations

import json
import re

from .prompts import CONDUCTOR
from .ratelimit import ResilientCaller
from .schemas import Plan, Step, TokenUsage

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_plan(text: str) -> Plan:
    match = _JSON_RE.search(text or "")
    if not match:
        return _fallback_plan()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return _fallback_plan()
    return Plan(
        task_type=str(data.get("task_type", "general")),
        difficulty=str(data.get("difficulty", "medium")),
        subtasks=list(data.get("subtasks", []) or []),
        direct_answer_possible=bool(data.get("direct_answer_possible", False)),
        routing_rationale=str(data.get("routing_rationale", "")),
    )


def _fallback_plan() -> Plan:
    return Plan(
        task_type="general",
        difficulty="medium",
        subtasks=["Solve the task"],
        direct_answer_possible=False,
        routing_rationale="Conductor output unparseable; using default sequential plan.",
    )


async def run_conductor(caller: ResilientCaller, task: str) -> tuple[Plan, Step]:
    outcome = await caller.call_role("conductor", system=CONDUCTOR, user=task)
    plan = _parse_plan(outcome.result.text)
    step = Step(
        step="conductor",
        role="conductor",
        model=outcome.model_used,
        routing_rationale=plan.routing_rationale,
        output=outcome.result.text,
        tokens=TokenUsage(
            **{"in": outcome.result.tokens_in, "out": outcome.result.tokens_out}
        ),
        latency_ms=outcome.latency_ms,
        error="; ".join(outcome.fallbacks) or None,
    )
    return plan, step

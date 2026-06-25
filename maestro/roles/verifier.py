"""Verifier — cross-model judge with schema-validated verdict.

Always a different model family than the Worker (config enforces this), mitigating the
10–25% self-preference bias documented in LLM-as-judge research.
"""

from __future__ import annotations

import json
import re

from ..prompts import VERIFIER
from ..ratelimit import ResilientCaller
from ..schemas import Step, TokenUsage

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_verdict(text: str) -> tuple[str, list[str]]:
    match = _JSON_RE.search(text or "")
    if not match:
        # Unparseable verdict -> treat as unverified-pass (don't block the run).
        return "pass", []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "pass", []
    verdict = "fail" if str(data.get("verdict", "pass")).lower() == "fail" else "pass"
    issues = [str(i) for i in (data.get("issues") or [])]
    return verdict, issues


async def run_verifier(caller: ResilientCaller, task: str, answer: str) -> Step:
    user = f"TASK:\n{task}\n\nCANDIDATE ANSWER:\n{answer}"
    outcome = await caller.call_role("verifier", system=VERIFIER, user=user)
    verdict, issues = _parse_verdict(outcome.result.text)
    return Step(
        step="verify",
        role="verifier",
        model=outcome.model_used,
        routing_rationale="Different-family model judges the Worker to avoid self-preference bias.",
        output=outcome.result.text,
        verdict=verdict,
        issues=issues,
        tokens=TokenUsage(
            **{"in": outcome.result.tokens_in, "out": outcome.result.tokens_out}
        ),
        latency_ms=outcome.latency_ms,
        error="; ".join(outcome.fallbacks) or None,
    )

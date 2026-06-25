#!/usr/bin/env python3
"""Honest benchmark harness: Conductor mode vs. single-model baseline.

Runs every task in tasks.jsonl through both modes, then asks a *held-out* judge model
to pick the better answer with randomized presentation order (to mitigate position bias).
Reports a win-rate plus token/latency cost — including where orchestration does NOT help.

Usage:
    python benchmarks/run_bench.py                 # uses mock provider if no keys set
    python benchmarks/run_bench.py --tasks benchmarks/tasks.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

# Allow `python benchmarks/run_bench.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maestro.config import get_config
from maestro.orchestrator import Orchestrator
from maestro.prompts import VERIFIER  # reuse skeptical judging tone
from maestro.ratelimit import ResilientCaller, TokenBucketLimiter
from maestro.providers import build_registry
from maestro.schemas import Mode

JUDGE_SYSTEM = (
    "You are an impartial judge. Given a task and two answers (A and B), reply with "
    "ONLY a JSON object: {\"winner\": \"A|B|tie\", \"reason\": \"one sentence\"}."
)


def load_tasks(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def judge(caller: ResilientCaller, task: str, a: str, b: str) -> str:
    user = f"TASK:\n{task}\n\nANSWER A:\n{a}\n\nANSWER B:\n{b}"
    outcome = await caller.call_role("verifier", system=JUDGE_SYSTEM, user=user)
    try:
        import re

        m = re.search(r"\{.*\}", outcome.result.text, re.DOTALL)
        return json.loads(m.group(0)).get("winner", "tie") if m else "tie"
    except Exception:
        return "tie"


async def main(tasks_path: Path) -> None:
    cfg = get_config()
    orch = Orchestrator(cfg)
    judge_caller = ResilientCaller(cfg, build_registry(cfg), TokenBucketLimiter(cfg))

    tasks = load_tasks(tasks_path)
    wins = {"conductor": 0, "single": 0, "tie": 0}
    cond_tokens = single_tokens = 0
    cond_ms = single_ms = 0

    print(f"Running {len(tasks)} tasks (conductor vs single)…\n")
    for t in tasks:
        cond = await orch.run(t["task"], mode=Mode.conductor)
        single = await orch.run(t["task"], mode=Mode.single)
        cond_tokens += cond.totals.tokens
        single_tokens += single.totals.tokens
        cond_ms += cond.totals.wall_ms
        single_ms += single.totals.wall_ms

        # Randomize A/B assignment to neutralize position bias.
        swap = random.random() < 0.5
        a, b = (
            (cond.final_answer, single.final_answer)
            if not swap
            else (single.final_answer, cond.final_answer)
        )
        winner = await judge(judge_caller, t["task"], a, b)
        if winner == "tie":
            wins["tie"] += 1
        else:
            picked_a = winner == "A"
            cond_won = picked_a != swap  # account for the swap
            wins["conductor" if cond_won else "single"] += 1
        print(f"  [{t['id']:<10}] winner={winner}")

    await orch.aclose()
    n = len(tasks)
    print("\n" + "=" * 50)
    print(f"Conductor wins : {wins['conductor']}/{n}")
    print(f"Single wins    : {wins['single']}/{n}")
    print(f"Ties           : {wins['tie']}/{n}")
    print("-" * 50)
    print(f"Conductor cost : {cond_tokens} tok, {cond_ms} ms")
    print(f"Single cost    : {single_tokens} tok, {single_ms} ms")
    print("=" * 50)
    print("\nNote: with the mock provider these numbers are illustrative only.")
    print("Set GROQ_API_KEY + GOOGLE_API_KEY for a real benchmark.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="benchmarks/tasks.jsonl")
    args = ap.parse_args()
    asyncio.run(main(Path(args.tasks)))

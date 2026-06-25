#!/usr/bin/env python3
"""Maestro CLI — run a single orchestration from the terminal.

Usage:
    python cli.py "Explain why the sky is blue" --mode conductor
    python cli.py "Write a Python LRU cache" --mode single --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from maestro.config import get_config, get_security
from maestro.decision_log import to_export_dict
from maestro.orchestrator import Orchestrator
from maestro.schemas import Mode
from maestro.security import InputRejected, sanitize_task


async def _main(task: str, mode: Mode, as_json: bool) -> int:
    sec = get_security()
    try:
        task = sanitize_task(task, sec.max_prompt_chars)
    except InputRejected as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    orch = Orchestrator(get_config())
    try:
        run = await orch.run(task, mode=mode)
    finally:
        await orch.aclose()

    if as_json:
        print(json.dumps(to_export_dict(run), indent=2))
    else:
        print("=" * 70)
        for step in run.steps:
            tag = step.verdict or step.step
            print(f"[{step.role:>11}] {step.model:<28} ({step.latency_ms} ms) {tag}")
        print("=" * 70)
        print(f"\nVERIFICATION: {run.verification_status}")
        print(f"TOTALS: {run.totals.calls} calls, {run.totals.tokens} tokens, "
              f"{run.totals.wall_ms} ms\n")
        print("FINAL ANSWER:\n")
        print(run.final_answer)
    return 0 if run.status == "complete" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Maestro orchestration CLI")
    parser.add_argument("task", help="The task/prompt to orchestrate")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default="conductor",
        help="Orchestration mode (default: conductor)",
    )
    parser.add_argument("--json", action="store_true", help="Emit the full decision-log JSON")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.task, Mode(args.mode), args.json)))


if __name__ == "__main__":
    main()

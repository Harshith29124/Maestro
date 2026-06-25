"""Decision-log persistence + JSON export.

The RunRecord (schemas.py) is the in-memory log. This module renders it to the
PRD's export schema and optionally persists runs to disk for later replay.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .schemas import RunRecord


def _default_runs_dir() -> Path:
    """Pick a writable runs directory.

    On serverless hosts (Vercel) the repo filesystem is read-only, so honor
    MAESTRO_RUNS_DIR, then fall back to a temp dir, then the repo. Persistence is
    best-effort and the full decision-log is always returned inline in the API
    response, so a read-only/ephemeral FS never breaks a run.
    """
    env = os.getenv("MAESTRO_RUNS_DIR")
    if env:
        return Path(env)
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        return Path(tempfile.gettempdir()) / "maestro_runs"
    return Path(__file__).resolve().parent.parent / "runs"


_RUNS_DIR = _default_runs_dir()


def to_export_dict(run: RunRecord) -> dict[str, Any]:
    """Render a RunRecord into the public decision-log schema (PRD B2)."""
    plan = run.plan.model_dump() if run.plan else {}
    return {
        "run_id": run.run_id,
        "task": run.task,
        "mode": run.mode.value,
        "status": run.status,
        "plan": plan,
        "steps": [
            {
                "step": s.step,
                "role": s.role,
                "model": s.model,
                "routing_rationale": s.routing_rationale,
                "output": s.output,
                "verdict": s.verdict,
                "issues": s.issues,
                "retry_triggered": s.retry_triggered,
                "tokens": {"in": s.tokens.in_, "out": s.tokens.out},
                "latency_ms": s.latency_ms,
                "error": s.error,
            }
            for s in run.steps
        ],
        "final_answer": run.final_answer,
        "verification_status": run.verification_status,
        "totals": run.totals.model_dump(),
    }


def save_run(run: RunRecord, runs_dir: Path | None = None) -> Path:
    target = runs_dir or _RUNS_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{run.run_id}.json"
    path.write_text(json.dumps(to_export_dict(run), indent=2), encoding="utf-8")
    return path


def load_run(run_id: str, runs_dir: Path | None = None) -> dict[str, Any] | None:
    target = runs_dir or _RUNS_DIR
    path = target / f"{run_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

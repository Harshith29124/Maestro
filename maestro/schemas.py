"""Pydantic models for the plan, decision-log steps, and run records.

These are the contract shared by the orchestrator, the API, and the dashboard.
The n8n workflow emits the *same* shapes so a single dashboard works against either backend.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def _new_id() -> str:
    return str(uuid4())


class Mode(str, Enum):
    conductor = "conductor"
    consensus = "consensus"
    single = "single"


class TokenUsage(BaseModel):
    in_: int = Field(0, alias="in")
    out: int = 0

    model_config = {"populate_by_name": True}


class Plan(BaseModel):
    task_type: str = "general"
    difficulty: Literal["trivial", "easy", "medium", "hard"] = "medium"
    subtasks: list[str] = Field(default_factory=list)
    direct_answer_possible: bool = False
    routing_rationale: str = ""


class Step(BaseModel):
    step: str
    role: str
    model: str
    routing_rationale: str = ""
    output: str = ""
    verdict: Optional[Literal["pass", "fail"]] = None
    issues: list[str] = Field(default_factory=list)
    retry_triggered: bool = False
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
    error: Optional[str] = None


class Totals(BaseModel):
    calls: int = 0
    tokens: int = 0
    wall_ms: int = 0
    fallbacks: list[str] = Field(default_factory=list)


class RunRecord(BaseModel):
    run_id: str = Field(default_factory=_new_id)
    task: str
    mode: Mode = Mode.conductor
    status: Literal["running", "complete", "failed"] = "running"
    plan: Optional[Plan] = None
    steps: list[Step] = Field(default_factory=list)
    final_answer: str = ""
    verification_status: Literal["verified", "unverified", "failed", "pending"] = "pending"
    totals: Totals = Field(default_factory=Totals)

    def add_step(self, step: Step) -> Step:
        self.steps.append(step)
        self.totals.calls += 1
        self.totals.tokens += step.tokens.in_ + step.tokens.out
        self.totals.wall_ms += step.latency_ms
        if step.error:
            self.totals.fallbacks.append(f"{step.step}:{step.model}:{step.error}")
        return step


class OrchestrateRequest(BaseModel):
    task: str = Field(..., min_length=1)
    mode: Mode = Mode.conductor
    max_rounds: Optional[int] = Field(None, ge=1, le=5)
    domain_hint: Optional[str] = None


class OrchestrateResponse(BaseModel):
    run_id: str
    final_answer: str
    verification_status: str
    mode: Mode
    totals: Totals
    decision_log: dict[str, Any]

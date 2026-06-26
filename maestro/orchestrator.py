"""Main orchestration flow with mode switching (conductor / consensus / single).

Conductor mode (default) is sequential and rate-limit-friendly:
    conductor -> thinker -> worker -> verifier -> [bounded retry] -> synthesizer

Consensus mode (MoA-lite): N parallel proposers -> 1 aggregator (capped to protect TPM).
Single mode: one model, the honest baseline for benchmarks.

Continue-on-fail: a single failed model call never crashes a run — it's recorded in the
decision-log and the orchestrator proceeds with partial results.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Awaitable, Callable, Optional

from .conductor import run_conductor
from .config import Config, get_config
from .prompts import CONSENSUS_AGGREGATOR, CONSENSUS_PROPOSER, WORKER
from .ratelimit import ResilientCaller, TokenBucketLimiter
from .providers import ProviderRegistry, build_registry
from .roles import run_synthesizer, run_thinker, run_verifier, run_worker
from .schemas import Mode, Plan, RunRecord, Step, TokenUsage

# Optional async hook fired after each step (used by the API for live WS updates).
StepHook = Callable[[Step, RunRecord], Awaitable[None]]


class Orchestrator:
    def __init__(
        self,
        config: Optional[Config] = None,
        registry: Optional[ProviderRegistry] = None,
    ):
        self._config = config or get_config()
        self._registry = registry or build_registry(self._config)
        self._limiter = TokenBucketLimiter(self._config)
        self._caller = ResilientCaller(self._config, self._registry, self._limiter)

    @property
    def limiter(self) -> TokenBucketLimiter:
        return self._limiter

    async def run(
        self,
        task: str,
        mode: Mode = Mode.conductor,
        on_step: Optional[StepHook] = None,
    ) -> RunRecord:
        run = RunRecord(task=task, mode=mode)
        start = time.monotonic()
        try:
            if mode == Mode.single:
                await self._run_single(run, task, on_step)
            elif mode == Mode.consensus:
                await self._run_consensus(run, task, on_step)
            else:
                await self._run_conductor(run, task, on_step)
            run.status = "complete"
        except Exception as exc:  # noqa: BLE001 - surface as failed run, never crash caller
            run.status = "failed"
            run.verification_status = "failed"
            if not run.final_answer:
                run.final_answer = f"Run failed: {exc}"
        run.totals.wall_ms = int((time.monotonic() - start) * 1000)
        return run

    # --- conductor mode ---------------------------------------------------
    async def _run_conductor(
        self, run: RunRecord, task: str, on_step: Optional[StepHook]
    ) -> None:
        plan, step = await run_conductor(self._caller, task)
        run.plan = plan
        await self._record(run, step, on_step)

        # Cheap path: trivial task the conductor says can be answered directly.
        # Still verify it with a different model; only accept on a passing verdict,
        # otherwise fall through to the full Thinker -> Worker -> Verifier pipeline.
        if plan.direct_answer_possible:
            worker_step = await run_worker(self._caller, task, strategy="Answer directly.")
            await self._record(run, worker_step, on_step)
            direct_verify = await run_verifier(self._caller, task, worker_step.output)
            direct_verify.step = "verify_direct"
            await self._record(run, direct_verify, on_step)
            if direct_verify.verdict == "pass":
                run.final_answer = worker_step.output
                run.verification_status = "verified"
                return
            # Direct answer failed verification — escalate to the full pipeline.

        think_step = await run_thinker(self._caller, task)
        await self._record(run, think_step, on_step)

        work_step = await run_worker(self._caller, task, think_step.output)
        await self._record(run, work_step, on_step)

        verify_step = await run_verifier(self._caller, task, work_step.output)
        await self._record(run, verify_step, on_step)

        verified = verify_step.verdict == "pass"
        if verify_step.verdict == "fail" and self._config.max_retries > 0:
            correction = "\n".join(verify_step.issues) or "Improve correctness and completeness."
            retry_step = await run_worker(
                self._caller, task, think_step.output, correction=correction
            )
            await self._record(run, retry_step, on_step)
            reverify = await run_verifier(self._caller, task, retry_step.output)
            reverify.step = "verify_retry"
            await self._record(run, reverify, on_step)
            work_step = retry_step
            verified = reverify.verdict == "pass"

        synth_step = await run_synthesizer(
            self._caller, task, think_step.output, work_step.output
        )
        await self._record(run, synth_step, on_step)

        run.final_answer = synth_step.output
        run.verification_status = "verified" if verified else "unverified"

    # --- consensus (MoA-lite) mode ---------------------------------------
    async def _run_consensus(
        self, run: RunRecord, task: str, on_step: Optional[StepHook]
    ) -> None:
        n = self._config.consensus_proposers
        # Draw distinct model families for real MoA diversity (Self-MoA shows mixing
        # *weak* models hurts, so the pool is curated, not random). Falls back to the
        # worker chain if no consensus_pool is configured.
        pool = self._config.roles.get("consensus_pool") or self._config.roles.get("worker", [])
        # Capped parallelism to protect TPM; proposers run concurrently but the
        # limiter still serializes within each model's RPM/TPM budget.
        proposals = await asyncio.gather(
            *(
                self._proposer(task, i, pool[i % len(pool)] if pool else None)
                for i in range(n)
            ),
            return_exceptions=True,
        )
        candidate_texts: list[str] = []
        for i, prop in enumerate(proposals):
            if isinstance(prop, Exception):
                run.add_step(
                    Step(
                        step=f"propose_{i}",
                        role="proposer",
                        model="n/a",
                        error=str(prop),
                    )
                )
                continue
            await self._record(run, prop, on_step)
            candidate_texts.append(prop.output)

        if not candidate_texts:
            raise RuntimeError("All consensus proposers failed.")

        # Randomize order to mitigate position bias in the aggregator.
        import random

        random.shuffle(candidate_texts)
        joined = "\n\n".join(
            f"CANDIDATE {i + 1}:\n{t}" for i, t in enumerate(candidate_texts)
        )
        outcome = await self._caller.call_role(
            "synthesizer",
            system=CONSENSUS_AGGREGATOR,
            user=f"TASK:\n{task}\n\n{joined}",
        )
        agg = Step(
            step="aggregate",
            role="aggregator",
            model=outcome.model_used,
            routing_rationale="Aggregator merges peer proposals (MoA-lite).",
            output=outcome.result.text,
            tokens=TokenUsage(
                **{"in": outcome.result.tokens_in, "out": outcome.result.tokens_out}
            ),
            latency_ms=outcome.latency_ms,
            error="; ".join(outcome.fallbacks) or None,
        )
        await self._record(run, agg, on_step)
        run.final_answer = agg.output
        run.verification_status = "unverified"

    async def _proposer(self, task: str, idx: int, model_name: str | None) -> Step:
        if model_name:
            # Pin this proposer to a specific family; the rest of the pool is its fallback.
            pool = self._config.roles.get("consensus_pool") or self._config.roles.get("worker", [])
            chain = [model_name] + [m for m in pool if m != model_name]
            outcome = await self._caller.call_models(
                chain, system=CONSENSUS_PROPOSER, user=task
            )
        else:
            outcome = await self._caller.call_role(
                "worker", system=CONSENSUS_PROPOSER, user=task
            )
        return Step(
            step=f"propose_{idx}",
            role="proposer",
            model=outcome.model_used,
            routing_rationale=f"Independent proposer ({model_name or 'worker chain'}) for consensus.",
            output=outcome.result.text,
            tokens=TokenUsage(
                **{"in": outcome.result.tokens_in, "out": outcome.result.tokens_out}
            ),
            latency_ms=outcome.latency_ms,
            error="; ".join(outcome.fallbacks) or None,
        )

    # --- single (baseline) mode ------------------------------------------
    async def _run_single(
        self, run: RunRecord, task: str, on_step: Optional[StepHook]
    ) -> None:
        outcome = await self._caller.call_role("worker", system=WORKER, user=task)
        step = Step(
            step="single",
            role="worker",
            model=outcome.model_used,
            routing_rationale="Single-model baseline for comparison.",
            output=outcome.result.text,
            tokens=TokenUsage(
                **{"in": outcome.result.tokens_in, "out": outcome.result.tokens_out}
            ),
            latency_ms=outcome.latency_ms,
            error="; ".join(outcome.fallbacks) or None,
        )
        await self._record(run, step, on_step)
        run.final_answer = step.output
        run.verification_status = "unverified"

    # --- helpers ----------------------------------------------------------
    async def _record(
        self, run: RunRecord, step: Step, on_step: Optional[StepHook]
    ) -> None:
        run.add_step(step)
        if on_step:
            await on_step(step, run)

    async def aclose(self) -> None:
        for provider in getattr(self._registry, "_providers", {}).values():
            try:
                await provider.aclose()
            except Exception:  # noqa: BLE001
                pass

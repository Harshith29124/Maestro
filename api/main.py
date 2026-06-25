"""Maestro FastAPI app: REST + WebSocket orchestration with security baked in.

Endpoints:
    GET  /                  -> dashboard (static)
    GET  /health            -> liveness + provider/security posture
    GET  /limits            -> current per-model rate-limiter snapshot
    POST /orchestrate       -> run an orchestration (auth + rate limit + input hardening)
    GET  /runs/{run_id}     -> fetch a persisted decision-log
    WS   /ws/orchestrate    -> run with live per-step streaming
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from maestro import __version__
from maestro.config import get_config, get_security
from maestro.decision_log import load_run, save_run, to_export_dict
from maestro.orchestrator import Orchestrator
from maestro.schemas import Mode, OrchestrateRequest, OrchestrateResponse, RunRecord, Step
from maestro.security import InputRejected, sanitize_task

from .security import (
    DEFAULT_CSP,
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
    SlidingWindowRateLimiter,
    build_rate_limiter,
    require_api_key,
    validate_production_security,
)

logger = logging.getLogger("maestro.api")
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

_orchestrator: Orchestrator | None = None
_rate_limiter = None  # primary (Upstash if configured, else in-memory)
_rate_backstop: SlidingWindowRateLimiter | None = None  # per-instance, used with Upstash


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _rate_limiter, _rate_backstop
    sec = get_security()
    for problem in validate_production_security(sec):
        logger.warning("SECURITY: %s", problem)
    _orchestrator = Orchestrator(get_config())
    _rate_limiter = build_rate_limiter(sec)
    # When the primary is a shared Redis limiter (fail-open), keep a per-instance
    # memory limiter as a backstop so a Redis outage can't fully open the API.
    _rate_backstop = (
        SlidingWindowRateLimiter(sec.rate_limit_per_minute, sec.rate_limit_per_day)
        if _rate_limiter.backend != "memory"
        else None
    )
    logger.info(
        "Maestro %s ready (auth=%s, mock=%s, ratelimit=%s)",
        __version__, sec.auth_enabled, sec.allow_mock, _rate_limiter.backend,
    )
    yield
    if _orchestrator:
        await _orchestrator.aclose()


app = FastAPI(title="Maestro", version=__version__, lifespan=lifespan)

# --- security middleware (order matters: outermost added last) ---
_sec = get_security()
app.add_middleware(SecurityHeadersMiddleware, csp=DEFAULT_CSP)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in _sec.cors_origins if o != "*"] or _sec.cors_origins,
    allow_credentials=False,  # we use API keys/headers, not cookies
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
    max_age=600,
)


async def _enforce_rate_limit(client_id: str) -> tuple[bool, int, str]:
    """Check primary (+ memory backstop). Returns (allowed, retry_after, scope)."""
    assert _rate_limiter is not None
    allowed, retry_after, scope = await _rate_limiter.check(client_id)
    if allowed and _rate_backstop is not None:
        allowed, retry_after, scope = _rate_backstop.check_sync(client_id)
    return allowed, retry_after, scope


async def _check_rate_limit(client_id: str) -> None:
    allowed, retry_after, scope = await _enforce_rate_limit(client_id)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({scope}). Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )


# ------------------------------------------------------------------ routes
@app.get("/health")
async def health() -> dict:
    sec = get_security()
    cfg = get_config()
    return {
        "status": "ok",
        "version": __version__,
        "auth_enabled": sec.auth_enabled,
        "mock_mode": sec.allow_mock,
        "environment": sec.env,
        "rate_limit_backend": _rate_limiter.backend if _rate_limiter else "unknown",
        "models": list(cfg.models.keys()),
    }


@app.get("/limits")
async def limits(client_id: str = Depends(require_api_key)) -> dict:
    assert _orchestrator is not None
    return {"provider_models": _orchestrator.limiter.snapshot()}


@app.post("/orchestrate", response_model=OrchestrateResponse)
async def orchestrate(
    payload: OrchestrateRequest,
    client_id: str = Depends(require_api_key),
) -> OrchestrateResponse:
    await _check_rate_limit(client_id)
    sec = get_security()
    try:
        task = sanitize_task(payload.task, sec.max_prompt_chars)
    except InputRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    assert _orchestrator is not None
    run = await _orchestrator.run(task, mode=payload.mode)
    try:
        save_run(run)
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        logger.warning("Failed to persist run %s: %s", run.run_id, exc)

    return OrchestrateResponse(
        run_id=run.run_id,
        final_answer=run.final_answer,
        verification_status=run.verification_status,
        mode=run.mode,
        totals=run.totals,
        decision_log=to_export_dict(run),
    )


@app.get("/runs/{run_id}")
async def get_run(run_id: str, client_id: str = Depends(require_api_key)) -> dict:
    data = load_run(run_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return data


@app.websocket("/ws/orchestrate")
async def ws_orchestrate(ws: WebSocket) -> None:
    """Live orchestration: client sends {task, mode, api_key?}, server streams steps."""
    await ws.accept()
    sec = get_security()
    try:
        req = await ws.receive_json()
    except Exception:
        await ws.close(code=1003)
        return

    # Auth over WS: validate the key from the first message.
    if sec.auth_enabled:
        key = (req.get("api_key") or "").strip()
        if key not in sec.api_keys:
            await ws.send_json({"type": "error", "detail": "Invalid API key."})
            await ws.close(code=1008)
            return
        client_id = f"key:{key[:6]}"
    else:
        client_id = f"ws:{ws.client.host if ws.client else 'unknown'}"

    # Rate limit WS runs too.
    allowed, retry_after, scope = await _enforce_rate_limit(client_id)
    if not allowed:
        await ws.send_json({"type": "error", "detail": f"Rate limit ({scope}). Retry in {retry_after}s."})
        await ws.close(code=1013)
        return

    try:
        task = sanitize_task(req.get("task", ""), sec.max_prompt_chars)
    except InputRejected as exc:
        await ws.send_json({"type": "error", "detail": str(exc)})
        await ws.close(code=1003)
        return

    mode = _safe_mode(req.get("mode"))

    async def on_step(step: Step, run: RunRecord) -> None:
        await ws.send_json({"type": "step", "step": step.model_dump(by_alias=True)})

    assert _orchestrator is not None
    try:
        await ws.send_json({"type": "start", "mode": mode.value})
        run = await _orchestrator.run(task, mode=mode, on_step=on_step)
        try:
            save_run(run)
        except Exception:  # noqa: BLE001
            pass
        await ws.send_json({"type": "complete", "decision_log": to_export_dict(run)})
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        await ws.send_json({"type": "error", "detail": str(exc)})
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def _safe_mode(raw: object) -> Mode:
    try:
        return Mode(str(raw))
    except (ValueError, TypeError):
        return Mode.conductor


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internals to clients.
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# Mount the dashboard last so API routes take precedence.
if _DASHBOARD_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")

"""API security: authentication, per-client rate limiting, security headers, CORS.

Designed for a public free-tier deployment (Vercel / Railway), where the threats are:
abusive callers burning the provider quota, missing auth, permissive CORS, and clickjacking.

NOTE on serverless: the in-memory rate limiter is per-process. On a single Railway
container it is globally accurate. On Vercel's serverless functions (many short-lived
instances) it is per-instance and best-effort. For a *globally* correct limit on Vercel,
set UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN (free tier) and Maestro automatically
uses a Redis-backed limiter shared across all instances. `build_rate_limiter()` picks the
backend; both expose the same async `check()` API.
"""

from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from maestro.config import SecuritySettings, get_security

# Request bodies above this are rejected outright (orchestration prompts are small).
MAX_BODY_BYTES = 64 * 1024


# --------------------------------------------------------------------------- auth
def _constant_time_in(candidate: str, allowed: list[str]) -> bool:
    """Constant-time membership check to avoid leaking key length/prefix via timing."""
    ok = False
    for key in allowed:
        if hmac.compare_digest(candidate, key):
            ok = True
    return ok


def extract_client_key(
    request: Request,
    x_api_key: str | None,
    authorization: str | None,
) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    """FastAPI dependency. Enforces auth when MAESTRO_API_KEYS is set.

    Returns a stable client identity used as the rate-limit bucket key.
    """
    sec = get_security()
    client_key = extract_client_key(request, x_api_key, authorization)

    if not sec.auth_enabled:
        # Auth disabled (local/dev) — bucket by client IP instead.
        return f"ip:{_client_ip(request)}"

    if not client_key or not _constant_time_in(client_key, sec.api_keys):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Bucket authenticated clients by a short, non-reversible handle of their key.
    return f"key:{client_key[:6]}…{client_key[-2:]}"


def _client_ip(request: Request) -> str:
    # Honor a single proxy hop (Railway/Vercel set X-Forwarded-For).
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------- rate limiting
@dataclass
class _Buckets:
    minute: deque[float]
    day: deque[float]


class SlidingWindowRateLimiter:
    """Per-client sliding-window limiter enforcing per-minute and per-day caps."""

    def __init__(self, per_minute: int, per_day: int):
        self.per_minute = per_minute
        self.per_day = per_day
        self._clients: dict[str, _Buckets] = defaultdict(
            lambda: _Buckets(deque(), deque())
        )

    def check_sync(self, client_id: str) -> tuple[bool, int, str]:
        """Returns (allowed, retry_after_seconds, scope)."""
        now = time.time()
        b = self._clients[client_id]

        _evict(b.minute, now - 60)
        _evict(b.day, now - 86400)

        if len(b.minute) >= self.per_minute:
            retry = max(1, int(60 - (now - b.minute[0])))
            return False, retry, "minute"
        if len(b.day) >= self.per_day:
            retry = max(1, int(86400 - (now - b.day[0])))
            return False, retry, "day"

        b.minute.append(now)
        b.day.append(now)
        return True, 0, ""

    async def check(self, client_id: str) -> tuple[bool, int, str]:
        return self.check_sync(client_id)

    @property
    def backend(self) -> str:
        return "memory"


def _evict(dq: deque[float], cutoff: float) -> None:
    while dq and dq[0] < cutoff:
        dq.popleft()


class UpstashRateLimiter:
    """Globally-consistent limiter backed by Upstash Redis (REST API).

    Uses fixed minute/day windows via atomic INCR + EXPIRE, pipelined into a single
    HTTPS round trip — no persistent connection, ideal for Vercel serverless. Counts are
    shared across every function instance, so the limit is enforced for real.

    Fails OPEN on a backend error (a Redis outage should not take the API down); the
    per-instance memory limiter still applies as a backstop in main.py.
    """

    def __init__(self, per_minute: int, per_day: int, url: str, token: str):
        self.per_minute = per_minute
        self.per_day = per_day
        self._url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=5.0
        )

    async def check(self, client_id: str) -> tuple[bool, int, str]:
        now = int(time.time())
        minute_key = f"rl:{client_id}:m:{now // 60}"
        day_key = f"rl:{client_id}:d:{now // 86400}"
        # Pipeline: INCR both keys, then set TTLs (idempotent; cheap).
        commands = [
            ["INCR", minute_key],
            ["EXPIRE", minute_key, "60"],
            ["INCR", day_key],
            ["EXPIRE", day_key, "86400"],
        ]
        try:
            resp = await self._client.post(f"{self._url}/pipeline", json=commands)
            resp.raise_for_status()
            results = resp.json()
            minute_count = int(results[0]["result"])
            day_count = int(results[2]["result"])
        except Exception:  # noqa: BLE001 - fail open; memory backstop still applies
            return True, 0, ""

        if minute_count > self.per_minute:
            return False, 60 - (now % 60), "minute"
        if day_count > self.per_day:
            return False, 86400 - (now % 86400), "day"
        return True, 0, ""

    @property
    def backend(self) -> str:
        return "upstash-redis"


def build_rate_limiter(sec: SecuritySettings):
    """Return a Redis-backed limiter if Upstash env vars are set, else in-memory."""
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return UpstashRateLimiter(
            sec.rate_limit_per_minute, sec.rate_limit_per_day, url, token
        )
    return SlidingWindowRateLimiter(sec.rate_limit_per_minute, sec.rate_limit_per_day)


# ----------------------------------------------------------------- middlewares
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds hardening headers to every response."""

    def __init__(self, app, csp: str):
        super().__init__(app)
        self._csp = csp

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        headers.setdefault("Content-Security-Policy", self._csp)
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if get_security().is_production:
            headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects oversized request bodies before they reach a handler."""

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            return Response("Payload too large.", status_code=413)
        return await call_next(request)


# Default CSP: the dashboard is self-hosted vanilla JS/CSS, so 'self' suffices.
DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


def validate_production_security(sec: SecuritySettings) -> list[str]:
    """Fail-fast warnings/errors surfaced at startup in production."""
    problems: list[str] = []
    if sec.is_production:
        if not sec.auth_enabled:
            problems.append("MAESTRO_API_KEYS is empty — API is unauthenticated in production.")
        if "*" in sec.cors_origins:
            problems.append("CORS allows '*' in production — set explicit origins.")
        if sec.allow_mock:
            problems.append("MAESTRO_ALLOW_MOCK is true in production — disable it.")
        if not sec.secret_key:
            problems.append("MAESTRO_SECRET_KEY is empty — set a strong secret.")
    return problems

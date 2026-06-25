"""API security: authentication, per-client rate limiting, security headers, CORS.

Designed for a public free-tier deployment (Vercel / Railway), where the threats are:
abusive callers burning the provider quota, missing auth, permissive CORS, and clickjacking.

NOTE on serverless: the in-memory rate limiter is per-process. On a single Railway
container it is globally accurate. On Vercel's serverless functions (multiple short-lived
instances) it is per-instance and best-effort — set MAESTRO_RATE_LIMIT_* conservatively
and put a platform/edge limiter or Redis in front for hard guarantees. This is documented
in the README's deployment section.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from dataclasses import dataclass

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

    def check(self, client_id: str) -> tuple[bool, int, str]:
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


def _evict(dq: deque[float], cutoff: float) -> None:
    while dq and dq[0] < cutoff:
        dq.popleft()


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
    "style-src 'self' 'unsafe-inline'; "
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

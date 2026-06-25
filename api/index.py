"""Vercel serverless entrypoint.

Vercel's @vercel/python runtime serves the ASGI `app` exported here. Note: Vercel's
serverless functions do NOT support WebSockets, so the live `/ws/orchestrate` stream is
unavailable there — the dashboard automatically falls back to the REST `/orchestrate`
endpoint. The in-process rate limiter is also per-instance on serverless; for hard limits
put Vercel's platform limiter or an external store (Redis/Upstash) in front. For the full
live-streaming experience, deploy on Railway (a single long-lived container) instead.
"""

from api.main import app  # noqa: F401  (re-exported for the Vercel runtime)

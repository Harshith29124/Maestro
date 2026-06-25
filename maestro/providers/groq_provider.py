"""Groq provider — OpenAI-compatible chat completions."""

from __future__ import annotations

from typing import Any

import httpx

from .base import ChatResult, ProviderError, RateLimitedError


class GroqProvider:
    name = "groq"

    def __init__(self, conf: dict[str, Any], api_key: str):
        self._base_url = conf.get("base_url", "https://api.groq.com/openai/v1").rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=conf.get("timeout_s", 60),
        )

    async def chat(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        timeout_s: float = 60,
    ) -> ChatResult:
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            resp = await self._client.post(
                "/chat/completions", json=payload, timeout=timeout_s
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Groq network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitedError(
                "Groq rate limit (429)", retry_after=_retry_after(resp)
            )
        if resp.status_code >= 400:
            raise ProviderError(f"Groq HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content", "") or ""
        usage = data.get("usage") or {}
        return ChatResult(
            text=text.strip(),
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            model=model_id,
            raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(resp: httpx.Response) -> float | None:
    val = resp.headers.get("retry-after")
    if val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None

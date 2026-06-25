"""Gemini provider — Google AI Studio generateContent REST API."""

from __future__ import annotations

from typing import Any

import httpx

from .base import ChatResult, ProviderError, RateLimitedError


class GeminiProvider:
    name = "gemini"

    def __init__(self, conf: dict[str, Any], api_key: str):
        self._base_url = conf.get(
            "base_url", "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=conf.get("timeout_s", 90))

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
        url = f"{self._base_url}/models/{model_id}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        try:
            resp = await self._client.post(
                url,
                json=payload,
                headers={"x-goog-api-key": self._api_key},
                timeout=timeout_s,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitedError(
                "Gemini rate limit (429)", retry_after=_retry_after(resp)
            )
        if resp.status_code >= 400:
            raise ProviderError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        text = _extract_text(data)
        usage = data.get("usageMetadata") or {}
        return ChatResult(
            text=text.strip(),
            tokens_in=int(usage.get("promptTokenCount", 0)),
            tokens_out=int(usage.get("candidatesTokenCount", 0)),
            model=model_id,
            raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _extract_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


def _retry_after(resp: httpx.Response) -> float | None:
    val = resp.headers.get("retry-after")
    if val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None

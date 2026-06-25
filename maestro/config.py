"""Loads config.yaml + environment into a single typed settings object.

Provider keys and app-security settings come from the environment (never the YAML),
so secrets stay out of the repo. The YAML holds the model pool and orchestration knobs.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [p.strip() for p in raw.split(",") if p.strip()]


class ModelSpec:
    def __init__(self, name: str, raw: dict[str, Any]):
        self.name = name
        self.provider: str = raw["provider"]
        self.api_id: str = raw["api_id"]
        self.rpm: int = int(raw.get("rpm", 30))
        self.tpm: int = int(raw.get("tpm", 8000))
        self.rpd: int = int(raw.get("rpd", 1000))


class Config:
    def __init__(self, raw: dict[str, Any]):
        self._raw = raw
        self.providers: dict[str, dict[str, Any]] = raw.get("providers", {})
        self.models: dict[str, ModelSpec] = {
            name: ModelSpec(name, spec) for name, spec in raw.get("models", {}).items()
        }
        self.roles: dict[str, list[str]] = raw.get("roles", {})
        self.orchestration: dict[str, Any] = raw.get("orchestration", {})
        self.defaults: dict[str, Any] = raw.get("defaults", {})

    # --- model resolution -------------------------------------------------
    def models_for_role(self, role: str) -> list[ModelSpec]:
        names = self.roles.get(role, [])
        return [self.models[n] for n in names if n in self.models]

    def provider_conf(self, provider: str) -> dict[str, Any]:
        return self.providers.get(provider, {})

    def api_key_for(self, provider: str) -> str | None:
        env_name = self.provider_conf(provider).get("api_key_env")
        return os.getenv(env_name) if env_name else None

    # --- orchestration knobs ---------------------------------------------
    @property
    def max_retries(self) -> int:
        return int(self.orchestration.get("max_retries", 1))

    @property
    def per_call_timeout_s(self) -> float:
        return float(self.orchestration.get("per_call_timeout_s", 60))

    @property
    def consensus_proposers(self) -> int:
        return int(self.orchestration.get("consensus_proposers", 2))

    @property
    def default_temperature(self) -> float:
        return float(self.defaults.get("temperature", 0.3))

    @property
    def default_max_tokens(self) -> int:
        return int(self.defaults.get("max_tokens", 1024))


class SecuritySettings:
    """App-level security knobs sourced purely from the environment."""

    def __init__(self) -> None:
        self.env: str = os.getenv("MAESTRO_ENV", "development").lower()
        self.api_keys: list[str] = _list_env("MAESTRO_API_KEYS")
        self.secret_key: str = os.getenv("MAESTRO_SECRET_KEY", "")
        self.cors_origins: list[str] = _list_env("MAESTRO_CORS_ORIGINS") or [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
        self.rate_limit_per_minute: int = int(os.getenv("MAESTRO_RATE_LIMIT_PER_MINUTE", "10"))
        self.rate_limit_per_day: int = int(os.getenv("MAESTRO_RATE_LIMIT_PER_DAY", "200"))
        self.max_prompt_chars: int = int(os.getenv("MAESTRO_MAX_PROMPT_CHARS", "8000"))
        self.allow_mock: bool = _bool_env("MAESTRO_ALLOW_MOCK", True)

    @property
    def is_production(self) -> bool:
        return self.env in {"production", "prod"}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)


@lru_cache(maxsize=1)
def get_config(path: str | None = None) -> Config:
    cfg_path = Path(path) if path else _REPO_ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config(raw)


@lru_cache(maxsize=1)
def get_security() -> SecuritySettings:
    return SecuritySettings()

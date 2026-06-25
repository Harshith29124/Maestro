"""Deterministic offline provider for demos, CI, and local runs without API keys.

It returns structured, role-aware responses so the full orchestration flow — including
JSON plan parsing and verifier verdicts — exercises end-to-end with no network.
"""

from __future__ import annotations

import json

from .base import ChatResult, estimate_tokens


class MockProvider:
    name = "mock"

    def __init__(self, masquerade_as: str = "mock"):
        # Reports as the provider it stands in for, so logs read naturally.
        self.name = masquerade_as

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
        text = self._respond(system, user)
        return ChatResult(
            text=text,
            tokens_in=estimate_tokens(system + user),
            tokens_out=estimate_tokens(text),
            model=f"mock:{model_id}",
            raw={"mock": True},
        )

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        return None

    def _respond(self, system: str, user: str) -> str:
        s = system.lower()
        if "conductor" in s or "emit a json plan" in s:
            return json.dumps(
                {
                    "task_type": "general",
                    "difficulty": "medium",
                    "subtasks": ["Understand the request", "Produce the answer"],
                    "direct_answer_possible": False,
                    "routing_rationale": (
                        "Medium-difficulty task: route to thinker for strategy, "
                        "worker for execution, and a cross-model verifier."
                    ),
                }
            )
        if "verifier" in s or "return a json verdict" in s:
            return json.dumps(
                {
                    "verdict": "pass",
                    "issues": [],
                    "rationale": "Mock verification: answer is coherent and on-topic.",
                }
            )
        if "thinker" in s or "strategy" in s or "outline" in s:
            return "Strategy: break the task into clear steps and address each directly."
        if "synthesi" in s:
            return f"[mock synthesis] Final answer addressing: {user[:160]}"
        # worker / default
        return f"[mock answer] Response to: {user[:200]}"

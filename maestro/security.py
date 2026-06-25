"""Input hardening shared by the CLI and API.

Keeps prompt-injection-adjacent footguns out of the orchestration layer: length caps,
control-character stripping, and null-byte rejection. This is defense-in-depth — the
real authn/rate-limiting lives in api/security.py.
"""

from __future__ import annotations

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class InputRejected(ValueError):
    """Raised when a task prompt fails validation."""


def sanitize_task(task: str, max_chars: int) -> str:
    """Validate and normalize a user task prompt.

    Raises InputRejected on empty input, null bytes, or over-length prompts.
    Strips control characters that have no business in a text prompt.
    """
    if task is None:
        raise InputRejected("Task is required.")
    task = task.strip()
    if not task:
        raise InputRejected("Task must not be empty.")
    if "\x00" in task:
        raise InputRejected("Task contains null bytes.")
    if len(task) > max_chars:
        raise InputRejected(
            f"Task too long: {len(task)} chars (limit {max_chars})."
        )
    return _CONTROL_CHARS.sub("", task)

"""Thin composition helpers for bundling atomic SKILL operations."""

from __future__ import annotations

from typing import Iterable

def compose_skill_script(commands: Iterable[str], *, wrap_in_progn: bool = True) -> str:
    """Compose atomic SKILL commands into one executable script string."""
    normalized = [command.strip() for command in commands if command and command.strip()]
    if not normalized:
        raise ValueError("At least one SKILL command is required")
    if len(normalized) == 1 and not wrap_in_progn:
        return normalized[0]
    if len(normalized) == 1 and normalized[0].startswith("progn("):
        return normalized[0]
    body = "\n".join(normalized)
    if not wrap_in_progn:
        return body
    return f"progn({body})"

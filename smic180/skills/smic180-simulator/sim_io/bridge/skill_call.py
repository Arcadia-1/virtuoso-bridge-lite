"""Centralized SKILL execution wrapper with logging and error handling.

Provides a consistent interface for all SKILL calls, replacing raw
client.execute_skill() with structured error checking and logging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from virtuoso_bridge import VirtuosoClient

logger = logging.getLogger(__name__)


class SkillExecutionError(RuntimeError):
    """Raised when a SKILL call returns errors and fail_ok=False."""


@dataclass
class SkillResult:
    """Structured result from a SKILL execution."""
    output: str
    ok: bool
    errors: str


def skill_exec(
    client: VirtuosoClient,
    code: str,
    *,
    timeout: int = 60,
    context: str = "",
    fail_ok: bool = False,
) -> SkillResult:
    """Execute SKILL code with logging and error checking.

    Parameters
    ----------
    client : VirtuosoClient
        Active Virtuoso bridge client.
    code : str
        SKILL code to execute.
    timeout : int
        Execution timeout in seconds.
    context : str
        Human-readable label for the call (used in logs and errors).
    fail_ok : bool
        If True, return errors instead of raising SkillExecutionError.

    Returns
    -------
    SkillResult with .output, .ok, and .errors fields.
    """
    label = context or code[:60]
    logger.debug("[skill_exec] %s: %s", label, code[:200])

    r = client.execute_skill(code, timeout=timeout)

    output = r.output or ""
    errors = r.errors if r.errors else ""

    ok = not errors
    result = SkillResult(output=output, ok=ok, errors=errors)

    if not ok and not fail_ok:
        raise SkillExecutionError(
            f"SKILL execution failed [{label}]: {errors}"
        )

    if not ok:
        logger.warning("[skill_exec] Non-fatal SKILL error [%s]: %s", label, errors)

    return result

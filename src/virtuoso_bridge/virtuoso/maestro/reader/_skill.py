"""Low-level SKILL execution helpers used across the reader package."""

from __future__ import annotations

import re
import time
import uuid

from virtuoso_bridge import VirtuosoClient


def _history_token(history: str) -> str:
    """Return a filesystem-safe token for history naming."""
    token = re.sub(r"[^A-Za-z0-9_.-]", "_", (history or "").strip())
    return token or "unknown"


def _unique_remote_wave_path(history: str) -> str:
    """Create a unique remote path to avoid cross-user file collisions."""
    ts_ms = int(time.time() * 1000)
    nonce = uuid.uuid4().hex[:8]
    return f"/tmp/vb_wave_{_history_token(history)}_{ts_ms}_{nonce}.txt"


def _q(client: VirtuosoClient, label: str, expr: str) -> str:
    """Execute SKILL, print a CIW breadcrumb, return raw output string."""
    wrapped = (
        f'let((rbResult) '
        f'rbResult = {expr} '
        f'printf("[%s read] {label}\\n" nth(2 parseString(getCurrentTime()))) '
        f'rbResult)'
    )
    r = client.execute_skill(wrapped)
    return r.output or ""


def _get_test(client: VirtuosoClient, session: str) -> str:
    """Get the first test name from a session."""
    r = client.execute_skill(f'maeGetSetup(?session "{session}")')
    raw = r.output or ""
    if raw and raw != "nil":
        m = re.findall(r'"([^"]+)"', raw)
        if m:
            return m[0]
    return ""

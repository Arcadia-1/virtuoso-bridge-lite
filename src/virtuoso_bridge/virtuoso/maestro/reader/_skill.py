"""Low-level SKILL execution helpers used across the reader package."""

from __future__ import annotations

import re
import time
import uuid

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string


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
    elabel = escape_skill_string(label)
    wrapped = (
        f'let((rbResult) '
        f'rbResult = {expr} '
        f'printf("[%s read] {elabel}\\n" nth(2 parseString(getCurrentTime()))) '
        f'rbResult)'
    )
    r = client.execute_skill(wrapped)
    return r.output or ""


def _get_test(client: VirtuosoClient, session: str) -> str:
    """Get the first test name from a session."""
    esess = escape_skill_string(session)
    r = client.execute_skill(f'maeGetSetup(?session "{esess}")')
    raw = r.output or ""
    if raw and raw != "nil":
        m = re.findall(r'"([^"]+)"', raw)
        if m:
            return m[0]
    return ""


def _session_lib_cell(client: VirtuosoClient, session: str) -> tuple[str, str]:
    """Library and cell of a Maestro session's cellview, where its results live.

    ``maeGetEnvOption(test ?option "lib")`` returns nil on IC23.1, so the
    session's own cellview is asked first; the test's options remain the
    fallback for versions without ``axlGetSessionLibName``.
    """
    def atom(expr: str) -> str:
        out = (client.execute_skill(expr).output or "").strip().strip('"')
        return "" if out in ("", "nil") else out

    lib = cell = ""
    if session:
        name = escape_skill_string(session)
        lib = atom(f'car(errset(axlGetSessionLibName("{name}")))')
        cell = atom(f'car(errset(axlGetSessionCellName("{name}")))')
    if not lib or not cell:
        test = _get_test(client, session)
        if test:
            s = f' ?session "{escape_skill_string(session)}"' if session else ""
            etest = escape_skill_string(test)
            lib = lib or atom(f'maeGetEnvOption("{etest}" ?option "lib"{s})')
            cell = cell or atom(f'maeGetEnvOption("{etest}" ?option "cell"{s})')
    return lib, cell


"""X11 dialog detection and dismissal via SSH (bypasses SKILL channel).

When a modal dialog blocks the Virtuoso CIW event loop, all execute_skill()
calls time out.  This module uses direct SSH + remote Python3/Xlib to find
and dismiss those dialogs without touching the SKILL channel.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Any

from virtuoso_bridge.env import load_vb_env
from virtuoso_bridge.transport.remote_paths import default_virtuoso_bridge_dir, resolve_client_id
from virtuoso_bridge.transport.ssh import SSHRunner

logger = logging.getLogger(__name__)

_HELPER_SCRIPT = Path(__file__).parent.parent / "resources" / "x11_dismiss_dialog.py"


def _get_display(display: str | None) -> str | None:
    """Resolve display: explicit arg > VB_DISPLAY env var > auto-detect (None)."""
    load_vb_env()
    if display:
        return display
    return os.getenv("VB_DISPLAY") or None


def _run(runner: SSHRunner | None, cmd: str, timeout: int):
    """Dispatch a shell command via SSH or local subprocess.

    Returns an object exposing ``.returncode`` / ``.stdout`` / ``.stderr``
    so the call sites can be agnostic to mode.
    """
    if runner is not None:
        return runner.run_command(cmd, timeout=timeout)
    import subprocess
    from types import SimpleNamespace
    try:
        r = subprocess.run(
            ["sh", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return SimpleNamespace(returncode=124, stdout="", stderr="timeout")
    except FileNotFoundError:
        return SimpleNamespace(returncode=127, stdout="", stderr="no shell")
    return SimpleNamespace(
        returncode=r.returncode,
        stdout=r.stdout or "",
        stderr=r.stderr or "",
    )


def _detect_remote_python(runner: SSHRunner | None, timeout: float = 10) -> str:
    """Find a Python interpreter (remote host or local).

    The X11 helper is intentionally Python 2/3 compatible because older EDA
    hosts often only provide Python 2.7.
    """
    r = _run(
        runner,
        'python3 --version 2>/dev/null && echo "CMD:python3" || '
        '(python --version 2>&1 | grep -q "Python" && echo "CMD:python") || '
        '(python2 --version 2>&1 | grep -q "Python" && echo "CMD:python2") || '
        'echo "CMD:NONE"',
        timeout=timeout,
    )
    for line in (r.stdout or "").splitlines():
        if line.strip().startswith("CMD:") and line.strip() != "CMD:NONE":
            return line.strip()[4:]
    return "python3"  # fallback; callers surface stderr/returncode as an error


def _ensure_helper(
    runner: SSHRunner | None,
    user: str,
    profile: str | None = None,
) -> str:
    """Resolve the path to the helper script.

    Remote: upload under the client-scoped bridge scratch directory.
    Local: the helper file is part of the installed package — return its
    on-disk path directly, no copy needed.
    """
    if runner is None:
        return str(_HELPER_SCRIPT)
    remote_dir = default_virtuoso_bridge_dir(user, "x11", resolve_client_id(profile))
    remote_path = f"{remote_dir}/x11_dismiss_dialog.py"
    runner.run_command(f"mkdir -p {remote_dir}")
    runner.upload(_HELPER_SCRIPT, remote_path)
    return remote_path


def find_dialogs(
    runner: SSHRunner | None,
    user: str,
    display: str | None = None,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """Find blocking dialog windows on the X11 display.

    Returns list of dicts: [{"window_id", "title", "x", "y", "w", "h"}, ...]
    """
    load_vb_env()
    script = _ensure_helper(runner, user, profile)
    py = _detect_remote_python(runner)
    resolved = _get_display(display)
    cmd = f"{py} {script}"
    if resolved:
        cmd += f" {resolved}"
    result = _run(runner, cmd, timeout=15)
    return _parse_result(result)


def list_windows(
    runner: SSHRunner | None,
    user: str,
    display: str | None = None,
    profile: str | None = None,
    top_level: bool = False,
) -> list[dict[str, Any]]:
    """Enumerate Virtuoso-related X11 windows without dismissing anything."""
    load_vb_env()
    script = _ensure_helper(runner, user, profile)
    py = _detect_remote_python(runner)
    resolved = _get_display(display)
    cmd = f"{py} {script} --list-windows --json"
    if top_level:
        cmd += " --top-level"
    if resolved:
        cmd += f" {resolved}"
    result = _run(runner, cmd, timeout=15)
    return _parse_result(result)


def inspect_dialogs(
    runner: SSHRunner | None, user: str, *, pid: int,
    display: str | None = None, ciw_window: str | None = None,
    profile: str | None = None, timeout: float = 15,
) -> dict[str, Any]:
    """Read-only inspection of one CIW process, without broad DISPLAY discovery."""
    import math

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("X11 inspection budget exhausted")
        return value

    if runner is None:
        script = str(_HELPER_SCRIPT)
        py = _detect_remote_python(None, timeout=remaining())
    else:
        # A content-addressed helper cannot be overwritten by another installed
        # Bridge version sharing the same profile scratch directory.
        digest = hashlib.sha256(_HELPER_SCRIPT.read_bytes()).hexdigest()[:16]
        key = (user, profile, digest)
        cached = getattr(runner, "_vb_dialog_helper", None)
        if cached is not None and cached[0] == key:
            script, py = cached[1:]
        else:
            root = default_virtuoso_bridge_dir(user, "x11", resolve_client_id(profile))
            script = f"{root}/dialog_inspect_{digest}.py"
            made = runner.run_command(f"mkdir -p {shlex.quote(root)}", timeout=remaining())
            if made.returncode != 0:
                raise RuntimeError(made.stderr or "cannot prepare X11 helper directory")
            uploaded = runner.upload(_HELPER_SCRIPT, script, timeout=remaining())
            if uploaded.returncode != 0:
                raise RuntimeError(uploaded.stderr or "cannot upload X11 inspection helper")
            py = _detect_remote_python(runner, timeout=remaining())
            runner._vb_dialog_helper = (key, script, py)
    cmd = f"{shlex.quote(py)} {shlex.quote(script)} --inspect-dialogs --pid {pid}"
    if ciw_window is not None:
        cmd += f" --ciw-window {shlex.quote(ciw_window)}"
    if display is not None:
        cmd += f" {shlex.quote(display)}"
    execution_budget = remaining()
    cmd += f" --timeout {execution_budget}"
    result = _run(runner, cmd, timeout=execution_budget)
    items = _parse_result(result)
    reports = [item for item in items if isinstance(item, dict) and "status" in item]
    if getattr(result, "returncode", 0) not in (0, 1) or len(items) != 1 or len(reports) != 1:
        return {
            "status": "indeterminate",
            "target": {"pid": pid, "display": display, "ciw_window": ciw_window},
            "dialogs": [],
            "diagnostics": ["X11 helper failed or returned an invalid inspection: " + str(items)],
        }
    return reports[0]


def dialog_close_exchange(
    runner: SSHRunner | None, user: str, payload: dict[str, Any], *,
    profile: str | None = None, timeout: float = 30,
) -> dict[str, Any]:
    """Use a version-scoped native helper; no shell/key fallback or retry."""
    import math

    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    deadline = time.monotonic() + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Dialog action budget exhausted")
        return value

    helper = _HELPER_SCRIPT.with_name("x11_dialog_close.py")
    if runner is None:
        script = str(helper)
        py = _detect_remote_python(None, timeout=remaining())
    else:
        digest = hashlib.sha256(_HELPER_SCRIPT.read_bytes() + helper.read_bytes()).hexdigest()[:16]
        key = (user, profile, digest)
        cached = getattr(runner, "_vb_dialog_close_helper", None)
        if cached is not None and cached[0] == key:
            script, py = cached[1:]
        else:
            root = default_virtuoso_bridge_dir(user, "x11", resolve_client_id(profile))
            directory = f"{root}/close_{digest}"
            made = runner.run_command(f"mkdir -p {shlex.quote(directory)}", timeout=remaining())
            if made.returncode:
                raise RuntimeError(made.stderr or "Cannot prepare dialog action helper")
            for local in (_HELPER_SCRIPT, helper):
                uploaded = runner.upload(local, f"{directory}/{local.name}", timeout=remaining())
                if uploaded.returncode:
                    raise RuntimeError(uploaded.stderr or "Cannot upload dialog action helper")
            script = f"{directory}/{helper.name}"
            py = _detect_remote_python(runner, timeout=remaining())
            runner._vb_dialog_close_helper = (key, script, py)
    request = dict(payload, timeout=remaining())
    encoded = base64.b64encode(json.dumps(request, ensure_ascii=True).encode("ascii")).decode("ascii")
    if len(encoded) > 16000:
        raise ValueError("Dialog action request exceeds bounded shell payload")
    command = (f"printf %s {shlex.quote(encoded)} | base64 -d | "
               f"{shlex.quote(py)} {shlex.quote(script)}")
    result = _run(runner, command, timeout=remaining())
    if len(result.stdout or "") > 8 * 1024 * 1024:
        raise RuntimeError(result.stderr or "Dialog action helper failed")
    answer = json.loads(result.stdout)
    if not isinstance(answer, dict):
        raise ValueError("Invalid dialog action reply")
    refused = answer.get("status") == "not_started" and answer.get("action_sent") is False
    if result.returncode and not (result.returncode == 2 and refused):
        raise RuntimeError(result.stderr or "Dialog action helper failed")
    return answer


def dismiss_window(
    runner: SSHRunner | None,
    user: str,
    window_id: str,
    *,
    action: str = "enter",
    display: str | None = None,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """Dismiss an explicit X11 window id with a requested key action."""
    load_vb_env()
    script = _ensure_helper(runner, user, profile)
    py = _detect_remote_python(runner)
    resolved = _get_display(display)
    cmd = (
        f"{py} {script} --dismiss-window {shlex.quote(window_id)} "
        f"--action {shlex.quote(action)}"
    )
    if resolved:
        cmd += f" {shlex.quote(resolved)}"
    result = _run(runner, cmd, timeout=15)
    return _parse_result(result)


def bootstrap_ciw(
    runner: SSHRunner | None,
    user: str,
    window_id: str,
    setup_path: str,
    *,
    display: str | None = None,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """Load the generated setup file in one explicit, verified CIW window."""
    load_vb_env()
    script = _ensure_helper(runner, user, profile)
    py = _detect_remote_python(runner)
    resolved = _get_display(display)
    cmd = (
        f"{py} {script} --bootstrap-window {shlex.quote(window_id)} "
        f"--setup-path {shlex.quote(setup_path)}"
    )
    if resolved:
        cmd += f" {resolved}"
    result = _run(runner, cmd, timeout=20)
    parsed = _parse_result(result)
    selected = [
        item for item in parsed
        if window_id in (
            item.get("requested_window_id"),
            item.get("window_id"),
            item.get("bootstrapped"),
        )
    ]
    # Auto-detection may probe stale or inaccessible DISPLAY values before it
    # reaches the selected CIW.  Keep those diagnostics, but never let them
    # mask the result for the window the caller explicitly requested.
    selected_success = [item for item in selected if "error" not in item]
    selected_error = [item for item in selected if "error" in item]
    remainder = [item for item in parsed if item not in selected]
    return selected_success + selected_error + remainder


def dismiss_dialogs(
    runner: SSHRunner | None,
    user: str,
    display: str | None = None,
    profile: str | None = None,
    *,
    allow_legacy_bulk: bool = False,
) -> list[dict[str, Any]]:
    """Find and dismiss all blocking dialog windows.

    Returns list of result dicts (found dialogs + dismissal results).
    """
    if not allow_legacy_bulk:
        return [{"error": "Bulk dialog dismissal is disabled. Inspect one CIW and use an explicit window/action; legacy bulk requires allow_legacy_bulk=True."}]
    load_vb_env()
    script = _ensure_helper(runner, user, profile)
    py = _detect_remote_python(runner)
    resolved = _get_display(display)
    env_prefix = ""
    for key in ("VB_SAVE_DIALOG_POLICY", "VB_SAVE_DIALOG_CONTEXT"):
        val = os.getenv(key)
        if val is not None and val != "":
            env_prefix += f"{key}={shlex.quote(val)} "

    cmd = f"{env_prefix}{py} {script} --dismiss"
    if resolved:
        cmd += f" {resolved}"
    result = _run(runner, cmd, timeout=15)
    return _parse_result(result)


def _parse_result(result) -> list[dict[str, Any]]:
    """Parse helper output and surface command failures as structured errors."""
    parsed = _parse_output(result.stdout)
    if parsed:
        return parsed

    returncode = getattr(result, "returncode", 0)
    stderr = (getattr(result, "stderr", "") or "").strip()
    if returncode:
        return [{
            "error": stderr or f"x11 helper command failed with return code {returncode}",
            "returncode": returncode,
        }]
    if stderr:
        return [{"error": stderr}]
    return []


def _parse_output(stdout: str) -> list[dict[str, Any]]:
    """Parse JSON-lines output from the helper script."""
    results = []
    for line in (stdout or "").strip().splitlines():
        line = line.strip()
        if line:
            try:
                results.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                logger.debug("Non-JSON line from helper: %s", line)
    return results

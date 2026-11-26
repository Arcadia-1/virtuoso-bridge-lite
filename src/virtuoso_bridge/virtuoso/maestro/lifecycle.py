"""Maestro session management: open, close, find.

Two modes:
- Background (open_session / close_session): for reading/writing config only.
- GUI (open_gui_session / close_gui_session): for running simulations.

Always use the GUI functions for simulation workflows.
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.maestro.reader.state import (
    get_session_state,
    list_session_states,
)
from virtuoso_bridge.virtuoso.x11 import _detect_remote_python

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# X11 key sending (for dismissing dialogs that block SKILL)
# ---------------------------------------------------------------------------

def _x11_run(runner, cmd: str, timeout: int = 5):
    """Run a shell command via SSH if *runner* is given, else locally.

    Returned object exposes ``.returncode`` / ``.stdout`` / ``.stderr``
    in both branches so callers can be agnostic.
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
        # No /bin/sh — Windows local mode for example.  X11 isn't usable
        # anyway in that environment; surface a no-op rather than crash.
        return SimpleNamespace(returncode=127, stdout="", stderr="no shell")
    return SimpleNamespace(
        returncode=r.returncode,
        stdout=r.stdout or "",
        stderr=r.stderr or "",
    )


def _resolve_connected_pid(client: VirtuosoClient, *, timeout: float = 5) -> int | None:
    """Resolve the verified connected CIW PID from ``client.dialogs``, else ``None``."""
    dialogs = getattr(client, "dialogs", None)
    if dialogs is None:
        return None
    connected_pid = getattr(dialogs, "connected_pid", None)
    if callable(connected_pid):
        try:
            pid = connected_pid(timeout=timeout)
        except Exception as exc:
            logger.warning(
                "Cannot verify connected Virtuoso PID for X11 dialog dismissal: %s", exc
            )
            return None
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            return pid
        return None
    target = getattr(dialogs, "target", None)
    target_pid = getattr(target, "pid", None)
    if (
        getattr(dialogs, "enabled", False)
        and isinstance(target_pid, int)
        and not isinstance(target_pid, bool)
        and target_pid > 0
    ):
        return target_pid
    return None


def _detect_virtuoso_display(runner, *, pid: int | None = None) -> str:
    """Detect the DISPLAY used by the verified Virtuoso process.

    Order: ``$VB_DISPLAY`` → ``/proc/<verified_pid>/environ``.
    Never falls back to ``pgrep | head -1`` across arbitrary Virtuoso processes.
    """
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise ValueError("pid must be a positive integer")

    display = os.getenv("VB_DISPLAY", "").strip()
    if display:
        if any(ch in display for ch in "\x00\r\n"):
            logger.warning("Ignoring VB_DISPLAY containing control characters")
            return ""
        return display

    if pid is None:
        logger.warning(
            "Cannot detect DISPLAY for X11 key sending without a verified Virtuoso PID"
        )
        return ""

    if runner is None:
        try:
            data = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            display = ""
        else:
            display = ""
            for chunk in data.split(b"\x00"):
                if chunk.startswith(b"DISPLAY="):
                    display = chunk.split(b"=", 1)[1].decode("utf-8", errors="replace").strip()
                    break
    else:
        r = _x11_run(
            runner,
            f"tr '\\0' '\\n' < /proc/{pid}/environ 2>/dev/null | grep '^DISPLAY=' | head -1",
            timeout=5,
        )
        line = (r.stdout or "").strip()
        display = line[len("DISPLAY="):].strip() if line.startswith("DISPLAY=") else ""

    if not display or any(ch in display for ch in "\x00\r\n"):
        logger.warning("Cannot detect DISPLAY for Virtuoso PID %d", pid)
        return ""
    return display


def _build_x11_key_script(events: list[tuple[int, int]]) -> str:
    """Build an LP64-safe, Python 2/3-compatible ctypes XTest key script."""
    event_tuples: list[str] = []
    for keysym, is_press in events:
        if isinstance(keysym, bool) or not isinstance(keysym, int) or keysym <= 0:
            raise ValueError(f"keysym must be a positive integer, got {keysym!r}")
        if is_press not in (0, 1):
            raise ValueError(f"is_press must be 0 or 1, got {is_press!r}")
        event_tuples.append(f"({keysym},{is_press})")
    events_literal = "[" + ",".join(event_tuples) + "]"
    return (
        "import ctypes,ctypes.util;"
        "x11_p=ctypes.util.find_library('X11');"
        "xtst_p=ctypes.util.find_library('Xtst');"
        "assert x11_p and xtst_p;"
        "xlib=ctypes.cdll.LoadLibrary(x11_p);"
        "xtst=ctypes.cdll.LoadLibrary(xtst_p);"
        "xlib.XOpenDisplay.argtypes=[ctypes.c_char_p];"
        "xlib.XOpenDisplay.restype=ctypes.c_void_p;"
        "xlib.XCloseDisplay.argtypes=[ctypes.c_void_p];"
        "xlib.XCloseDisplay.restype=ctypes.c_int;"
        "xlib.XFlush.argtypes=[ctypes.c_void_p];"
        "xlib.XFlush.restype=ctypes.c_int;"
        "xlib.XKeysymToKeycode.argtypes=[ctypes.c_void_p,ctypes.c_ulong];"
        "xlib.XKeysymToKeycode.restype=ctypes.c_uint;"
        "xtst.XTestFakeKeyEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_ulong];"
        "xtst.XTestFakeKeyEvent.restype=ctypes.c_int;"
        "dpy=xlib.XOpenDisplay(None);"
        "dpy and ("
        "lambda d: ("
        "[xtst.XTestFakeKeyEvent(d,kc,pr,0) for ks,pr in "
        + events_literal
        + " for kc in (xlib.XKeysymToKeycode(d,ks),) if kc],"
        "xlib.XFlush(d),"
        "xlib.XCloseDisplay(d)"
        "))(dpy)"
    )


def _send_x11_events(runner, events: list[tuple[int, int]], *, pid: int | None = None) -> None:
    """Send XTest key press/release events to the verified Virtuoso X11 display."""
    display = _detect_virtuoso_display(runner, pid=pid)
    if not display:
        return
    py = _detect_remote_python(runner, timeout=5)
    script = _build_x11_key_script(events)
    cmd = f"DISPLAY={shlex.quote(display)} {shlex.quote(py)} -c {shlex.quote(script)}"
    _x11_run(runner, cmd, timeout=5)


def _send_x11_key(runner, keysym: int, *, pid: int | None = None) -> None:
    """Send a single keypress to the verified Virtuoso X11 display."""
    _send_x11_events(runner, [(keysym, 1), (keysym, 0)], pid=pid)


def _send_x11_alt_n(runner, *, pid: int | None = None) -> None:
    """Send Alt+N (No/Don't Save) to the verified Virtuoso X11 display."""
    _send_x11_events(
        runner,
        [(0xFFE9, 1), (0x006E, 1), (0x006E, 0), (0xFFE9, 0)],
        pid=pid,
    )


# ---------------------------------------------------------------------------
# Cellview memory management
# ---------------------------------------------------------------------------

def _purge_maestro_cellviews(client: VirtuosoClient, *, timeout: int = 60) -> None:
    """Purge all maestro cellviews from Virtuoso's virtual memory.

    After hiCloseWindow + maeCloseSession, the cellview may still be
    cached in memory with an internal edit lock. dbPurge forces it out,
    allowing another cell to be opened in edit mode.
    """
    client.execute_skill('''
foreach(cv dbGetOpenCellViews()
  when(cv~>viewName == "maestro"
    errset(dbPurge(cv))))
''', timeout=timeout)


def _purge_maestro_cellview(
    client: VirtuosoClient, lib: str, cell: str, view: str, *, timeout: int = 60,
) -> None:
    """Purge one exact Maestro cellview after its session is confirmed closed."""
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_view = escape_skill_string(view)
    result = client.execute_skill(f'''
foreach(cv dbGetOpenCellViews()
  when(cv~>libName == "{escaped_lib}" &&
       cv~>cellName == "{escaped_cell}" &&
       cv~>viewName == "{escaped_view}"
    errset(dbPurge(cv))))
''', timeout=timeout)
    if result.errors:
        raise RuntimeError(
            f"dbPurge failed for {lib}/{cell}/{view}: {result.errors}"
        )


# ---------------------------------------------------------------------------
# Session state detection
# ---------------------------------------------------------------------------

def _get_session_windows(client: VirtuosoClient) -> list[dict]:
    """Get all ADE windows (Assembler and Explorer) with their session and state.

    Returns list of dicts with keys:
        session, window_num, mode ("editing"/"reading"/"unknown"),
        modified (bool/None),
        ade_type ("assembler"/"explorer"), title
    """
    return [
        {
            "session": state.session,
            "window_num": state.window_num,
            "mode": state.access,
            "modified": state.unsaved,
            "ade_type": state.application,
            "lib": state.lib,
            "cell": state.cell,
            "view": state.view,
            "title": state.title or "",
            "diagnostics": state.diagnostics,
        }
        for state in list_session_states(client)
        if state.window_num is not None and state.context in {"gui", "unknown"}
    ]


def _close_background_sessions(client: VirtuosoClient) -> list[str]:
    """Close all non-GUI sessions (background + zombie). Returns closed session names.

    A no-window inventory cannot distinguish deliberate background sessions
    from stale GUI sessions. Close attempts are verified; any unclosed entry
    blocks callers from opening another GUI session.
    """
    states = list_session_states(client)
    sessions = [state.session for state in states
                if state.context == "headless" and state.session]
    closed = []
    for s in sessions:
        escaped_s = escape_skill_string(s)
        result = client.execute_skill(f'maeCloseSession(?session "{escaped_s}" ?forceClose t)')
        if result.errors:
            raise RuntimeError(f"Could not close headless session {s}: {result.errors}")
        if get_session_state(client, s).context == "not_found":
            closed.append(s)
            logger.info("Closed headless session: %s", s)
        else:
            raise RuntimeError(f"Headless session {s} still exists after maeCloseSession.")
    return closed


# ---------------------------------------------------------------------------
# Background session (read/write config only)
# ---------------------------------------------------------------------------

def open_session(client: VirtuosoClient, lib: str, cell: str) -> str:
    """Open maestro in background via maeOpenSetup. Returns session string."""
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    r = client.execute_skill(
        f'let((session) session = maeOpenSetup("{escaped_lib}" "{escaped_cell}" "maestro") '
        f'printf("[%s maeOpenSetup] %s/%s  session=%s\\n" nth(2 parseString(getCurrentTime())) '
        f'"{escaped_lib}" "{escaped_cell}" session) '
        f'session)')
    if getattr(r, "errors", None) or getattr(r, "ok", True) is False:
        raise RuntimeError(f"maeOpenSetup failed for {lib}/{cell}: {getattr(r, 'errors', None)}")
    session = (r.output or "").strip('"')
    if not session or session in ("nil", "t"):
        raise RuntimeError(f"maeOpenSetup failed for {lib}/{cell}")
    return session


def close_session(client: VirtuosoClient, session: str) -> None:
    """Close a background maestro session via maeCloseSession.

    Wraps the close + log in ``progn`` so SKILL evaluates both as a
    sequence rather than mis-parsing the trailing ``printf`` token as a
    function applied to the close result (which silently swallows the
    error and leaves the session alive).
    """
    escaped_session = escape_skill_string(session)
    result = client.execute_skill(
        'progn('
        f'maeCloseSession(?session "{escaped_session}" ?forceClose t) '
        f'printf("[%s maeCloseSession] session=%s closed\\n" '
        f'nth(2 parseString(getCurrentTime())) "{escaped_session}"))'
    )
    errors = getattr(result, "errors", None) or []
    if errors or getattr(result, "ok", True) is False:
        detail = "; ".join(str(err) for err in errors) if errors else (
            getattr(result, "output", "") or "unknown error"
        )
        raise RuntimeError(f"maeCloseSession failed for {session}: {detail}")


def find_open_session(client: VirtuosoClient) -> str | None:
    """Find the first active session with a valid test. Returns session string or None.

    "Valid test" means ``maeGetSetup`` returns non-nil for the session,
    i.e. the maestro view has at least one test configured.  Callers
    looking for "the session of the cell I just opened" — including
    empty maestro views — should use :func:`_find_session_for_cell`
    instead.
    """
    raw = client.execute_skill('''
let((result)
  result = nil
  foreach(s maeGetSessions()
    unless(result
      when(maeGetSetup(?session s)
        result = s
      )
    )
  )
  result
)
''').output or ""
    session = raw.strip('"')
    if session and session != "nil":
        return session
    return None


def _find_session_for_cell(client: VirtuosoClient, lib: str, cell: str
                           ) -> str | None:
    """Return the GUI session string whose ADE window is for ``lib``/``cell``.

    Unlike :func:`find_open_session`, this does not require the maestro
    to contain any tests — useful right after ``deOpenCellView`` opens
    a fresh / empty view.  Matches by window title, which contains
    both the library and cell names for ADE Assembler / Explorer.

    Returns ``None`` if no matching window is found.
    """
    matches = [
        w for w in _get_session_windows(client)
        if w["lib"] == lib and w["cell"] == cell and w["view"] == "maestro"
    ]
    if len(matches) > 1:
        raise RuntimeError(f"More than one ADE window matches {lib}/{cell}/maestro.")
    return matches[0]["session"] if matches else None


# ---------------------------------------------------------------------------
# GUI session (required for simulation)
# ---------------------------------------------------------------------------

def open_gui_session(client: VirtuosoClient, lib: str, cell: str,
                     *, timeout: int = 60) -> str:
    """Open maestro in GUI mode, ready for simulation. Returns session string.

    Handles all edge cases safely:
    1. Closes any background sessions (they hold lock files)
    2. If an Editing GUI session exists for this cell, reuses it
    3. If a confirmed-clean Reading GUI session exists, closes it
    4. Opens a fresh GUI directly in editable mode if needed

    Existing windows with unknown access/unsaved state block the operation.
    The helper never assumes that an unrecognized title means read-only.

    `timeout` (default 60s) bounds the deOpenCellView SKILL call.
    The previous hard-coded 10s was below the P50 of cold maestro opens
    we observed (15-30s for fresh views, longer when results are being
    indexed) and surfaced as a "Socket timeout after 10s" RuntimeError.

    Returns the session string (e.g. "fnxSession3").
    """
    # Step 1: close background sessions
    closed_bg = _close_background_sessions(client)
    if closed_bg:
        logger.info("Closed background sessions: %s", closed_bg)

    # Step 2: verify all existing GUI sessions and check for a reusable
    # editable target *before* closing any windows.
    windows = _get_session_windows(client)

    for w in windows:
        if w["mode"] == "unknown" or w["modified"] is None or not w["session"]:
            detail = "; ".join(w.get("diagnostics") or ())
            raise RuntimeError(
                f"Cannot safely replace ADE window {w['window_num']}: its state is unknown."
                + (f" {detail}" if detail else "")
            )

    matching_editable = [
        w for w in windows
        if w["lib"] == lib and w["cell"] == cell and w["view"] == "maestro"
        and w["mode"] == "editing"
    ]
    if len(matching_editable) > 1:
        raise RuntimeError(
            f"More than one editable ADE window matches {lib}/{cell}/maestro."
        )
    if matching_editable:
        # Already editable for our cell — reuse without disturbing other windows
        logger.info("Reusing existing editable session: %s", matching_editable[0]["session"])
        return matching_editable[0]["session"]

    for w in windows:
        if w["modified"] and w["mode"] != "editing":
            raise RuntimeError(
                f"Session {w['session']} is read-only but reports unsaved changes; "
                "refusing to promote or discard it implicitly."
            )

    for w in windows:
        is_target = (
            w["lib"] == lib and w["cell"] == cell and w["view"] == "maestro"
        )
        # Close windows that are:
        # - for a different cell (must release edit lock)
        # - for our cell but in reading mode
        logger.info("Closing session %s (%s, target=%s)", w["session"], w["mode"], is_target)
        close_gui_session(client, w["session"], save=True)

    # Step 3: open in editable mode.
    # deOpenCellView with mode "a" opens editable. From a clean state
    # (no residual sessions), this opens Assembler by default.
    # Do NOT call maeOpenSetup afterwards — it creates a second
    # background session with its own lock, causing 8127 on next open.
    logger.info("Opening GUI (editable): %s/%s/maestro", lib, cell)
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    r = client.execute_skill(
        f'deOpenCellView("{escaped_lib}" "{escaped_cell}" "maestro" "maestro" nil "a")',
        timeout=timeout)
    if r.errors or not r.output or r.output.strip() in ("nil", ""):
        raise RuntimeError(f"deOpenCellView failed for {lib}/{cell}/maestro: {r.errors}")

    # Find the new session by matching the cell we just opened.  Do not
    # use find_open_session here — it filters on maeGetSetup, so a fresh
    # / empty maestro (no tests yet) is invisible to it and would surface
    # as a misleading "No session found after opening GUI".
    session = _find_session_for_cell(client, lib, cell)
    if not session:
        raise RuntimeError(
            f"No ADE window for {lib}/{cell} after deOpenCellView — "
            "the call returned but no matching window appeared; check "
            f"that {cell!r} actually has a 'maestro' view in library {lib!r}")
    logger.info("Opened GUI session: %s", session)
    return session


def close_gui_session(client: VirtuosoClient, session: str,
                      save: bool = True, *, timeout: int = 60) -> None:
    """Close a GUI maestro session safely.

    Checks window state before closing:
    - Editing with changes: saves first (if save=True), then closes
    - Editing without changes: closes directly
    - Reading with changes: block; never promote or discard implicitly
    - Reading without changes: closes directly
    - Unknown access/unsaved state: block

    Args:
        save: if True and an editable session has unsaved changes, save
              and verify it is clean before closing. If False, discarding
              a positively identified modified window is explicit.
        timeout: budget (seconds) for each blocking SKILL call in the
              close path (hiCloseWindow and dbPurge).
              Default 60s; previously hard-coded 10-15s, which was
              below P50 of slow operations on a busy session.
    """
    windows = _get_session_windows(client)
    matches = [w for w in windows if w["session"] == session]
    if len(matches) > 1:
        raise RuntimeError(f"More than one ADE window is bound to session {session}.")
    target_window = matches[0] if matches else None

    if target_window is None:
        state = get_session_state(client, session)
        if state.context == "not_found":
            return
        if state.context != "headless":
            raise RuntimeError(
                f"Cannot safely close session {session}: context is {state.context}."
            )
        logger.info("No GUI window for %s, closing headless session", session)
        escaped_session = escape_skill_string(session)
        result = client.execute_skill(
            f'maeCloseSession(?session "{escaped_session}" ?forceClose t)'
        )
        if result.errors:
            raise RuntimeError(f"maeCloseSession failed for {session}: {result.errors}")
        if get_session_state(client, session).context != "not_found":
            raise RuntimeError(f"Session {session} still exists after maeCloseSession.")
        return

    if target_window["mode"] == "unknown" or target_window["modified"] is None:
        raise RuntimeError(
            f"Cannot safely close session {session}: access or unsaved state is unknown."
        )

    if target_window["modified"] and save:
        if target_window["mode"] == "editing":
            # Editing* — save, then close
            logger.info("Saving modified Editing session %s", session)
            escaped_session = escape_skill_string(session)
            result = client.execute_skill(f'maeSaveSetup(?session "{escaped_session}")')
            if result.errors:
                raise RuntimeError(f"maeSaveSetup failed for {session}: {result.errors}")
            refreshed = [w for w in _get_session_windows(client) if w["session"] == session]
            if len(refreshed) != 1 or refreshed[0]["modified"] is not False:
                raise RuntimeError(
                    f"Session {session} was not confirmed clean after maeSaveSetup; refusing close."
                )
            target_window = refreshed[0]
        else:
            raise RuntimeError(
                f"Session {session} is read-only but reports unsaved changes; "
                "refusing to promote or discard it implicitly."
            )

    _close_gui_window(client, target_window, timeout=timeout)

    # Purge only the closed target from memory to release its internal edit
    # lock. Purging every Maestro cellview could disturb unrelated windows.
    _purge_maestro_cellview(
        client,
        target_window["lib"],
        target_window["cell"],
        target_window["view"],
        timeout=timeout,
    )
    logger.info("Closed GUI session: %s", session)


def _close_gui_window(client: VirtuosoClient, window_info: dict,
                      *, timeout: int = 60) -> None:
    """Close a GUI window, handling save dialogs safely.

    If the window has unsaved changes (*), hiCloseWindow pops a save
    dialog that blocks the SKILL channel. We pre-empt this by starting
    an X11 key-sender in a background thread BEFORE calling hiCloseWindow.
    The thread sends Escape (for Save As) or Alt+N (for Yes/No) to
    dismiss the dialog as soon as it appears.
    """
    import threading
    import time as _time

    wnum = window_info["window_num"]
    will_pop_dialog = window_info["modified"] is True

    dismiss_thread = None
    if will_pop_dialog:
        # X11 belongs to the GUI host.  Fall back to the legacy runner for
        # lightweight clients that predate the split GUI/daemon roles.
        runner = getattr(client, "gui_runner", None)
        if runner is None:
            runner = client.ssh_runner
        ciw_pid = _resolve_connected_pid(client, timeout=min(timeout, 5))
        if ciw_pid is None:
            logger.warning(
                "Skipping X11 Alt+N dismissal for modified window %d: connected Virtuoso PID "
                "could not be verified.",
                wnum,
            )
        else:
            def _dismiss_save_dialog():
                """Send Alt+N after a short delay to dismiss save dialog."""
                _time.sleep(0.5)
                # Alt+N selects "No" (Don't Save) on save confirmation dialogs.
                # Escape only cancels the dialog without closing the window.
                _send_x11_alt_n(runner, pid=ciw_pid)

            dismiss_thread = threading.Thread(target=_dismiss_save_dialog, daemon=True)
            dismiss_thread.start()
            logger.info("Started dismiss thread for modified window %d (pid=%d)", wnum, ciw_pid)

    result = client.execute_skill(f'''
let((w)
  foreach(win hiGetWindowList()
    when(win~>windowNum == {wnum} w = win))
  when(w hiCloseWindow(w)))
''', timeout=timeout)

    if dismiss_thread is not None:
        dismiss_thread.join(timeout=10)

    if result.errors:
        raise RuntimeError(
            f"hiCloseWindow failed for Maestro window {wnum}: {result.errors}"
        )

    session = window_info["session"]
    state = get_session_state(client, session)
    if state.context == "headless":
        escaped_session = escape_skill_string(session)
        result = client.execute_skill(
            f'maeCloseSession(?session "{escaped_session}" ?forceClose t)'
        )
        if result.errors:
            raise RuntimeError(f"maeCloseSession failed for {session}: {result.errors}")
        state = get_session_state(client, session)
    if state.context != "not_found":
        raise RuntimeError(
            f"Session {session} still has context {state.context} after GUI close; "
            "refusing to purge cellviews."
        )

"""Maestro session management: open, close, find.

Two modes:
- Background (open_session / close_session): for reading/writing config only.
- GUI (open_gui_session / close_gui_session): for running simulations.

Always use the GUI functions for simulation workflows.
"""

import logging
import re

from virtuoso_bridge import VirtuosoClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# X11 key sending (for dismissing dialogs that block SKILL)
# ---------------------------------------------------------------------------

def _detect_virtuoso_display(runner) -> str:
    """Detect the DISPLAY used by the Virtuoso process."""
    import os
    display = os.getenv("VB_DISPLAY", "")
    if display:
        return display
    r = runner.run_command(
        'strings /proc/$(pgrep -u $(whoami) -f "64bit/virtuoso" | head -1)/environ 2>/dev/null '
        '| grep ^DISPLAY= | head -1',
        timeout=5)
    display = (r.stdout or "").strip().replace("DISPLAY=", "")
    if not display:
        logger.warning("Cannot detect DISPLAY for X11 key sending")
    return display


def _send_x11_key(runner, keysym: int) -> None:
    """Send a single keypress to the Virtuoso X11 display via SSH."""
    display = _detect_virtuoso_display(runner)
    if not display:
        return
    runner.run_command(
        f'DISPLAY={display} python2.7 -c "'
        f'import ctypes,ctypes.util;'
        f'xlib=ctypes.cdll.LoadLibrary(ctypes.util.find_library(chr(88)+chr(49)+chr(49)));'
        f'xtst=ctypes.cdll.LoadLibrary(ctypes.util.find_library(chr(88)+chr(116)+chr(115)+chr(116)));'
        f'dpy=xlib.XOpenDisplay(None);'
        f'kc=xlib.XKeysymToKeycode(dpy,{keysym});'
        f'xtst.XTestFakeKeyEvent(dpy,kc,True,0);'
        f'xtst.XTestFakeKeyEvent(dpy,kc,False,0);'
        f'xlib.XFlush(dpy);xlib.XCloseDisplay(dpy)"',
        timeout=5)


def _send_x11_alt_n(runner) -> None:
    """Send Alt+N (No/Don't Save) to the Virtuoso X11 display."""
    display = _detect_virtuoso_display(runner)
    if not display:
        return
    runner.run_command(
        f'DISPLAY={display} python2.7 -c "'
        f'import ctypes,ctypes.util;'
        f'xlib=ctypes.cdll.LoadLibrary(ctypes.util.find_library(chr(88)+chr(49)+chr(49)));'
        f'xtst=ctypes.cdll.LoadLibrary(ctypes.util.find_library(chr(88)+chr(116)+chr(115)+chr(116)));'
        f'dpy=xlib.XOpenDisplay(None);'
        f'ka=xlib.XKeysymToKeycode(dpy,0xffe9);'
        f'kn=xlib.XKeysymToKeycode(dpy,0x006e);'
        f'xtst.XTestFakeKeyEvent(dpy,ka,True,0);'
        f'xtst.XTestFakeKeyEvent(dpy,kn,True,0);'
        f'xtst.XTestFakeKeyEvent(dpy,kn,False,0);'
        f'xtst.XTestFakeKeyEvent(dpy,ka,False,0);'
        f'xlib.XFlush(dpy);xlib.XCloseDisplay(dpy)"',
        timeout=5)


# ---------------------------------------------------------------------------
# Cellview memory management
# ---------------------------------------------------------------------------

def _purge_maestro_cellviews(client: VirtuosoClient) -> None:
    """Purge all maestro cellviews from Virtuoso's virtual memory.

    After hiCloseWindow + maeCloseSession, the cellview may still be
    cached in memory with an internal edit lock. dbPurge forces it out,
    allowing another cell to be opened in edit mode.
    """
    client.execute_skill('''
foreach(cv dbGetOpenCellViews()
  when(cv~>viewName == "maestro"
    errset(dbPurge(cv))))
''', timeout=10)


# ---------------------------------------------------------------------------
# Session state detection
# ---------------------------------------------------------------------------

def _get_session_windows(client: VirtuosoClient) -> list[dict]:
    """Get all ADE windows (Assembler and Explorer) with their session and state.

    Returns list of dicts with keys:
        session, window_num, mode ("editing"/"reading"), modified (bool),
        ade_type ("assembler"/"explorer"), title
    """
    r = client.execute_skill('''
let((result)
  result = list()
  foreach(w hiGetWindowList()
    let((s name)
      s = car(errset(axlGetWindowSession(w)))
      name = hiGetWindowName(w)
      when(s && name
        result = cons(list(s w~>windowNum name) result))))
  result)
''')
    raw = (r.output or "").strip()
    if not raw or raw == "nil":
        return []

    results = []
    # Parse: (("session" num "title") ...)
    for m in re.finditer(r'\("([^"]+)"\s+(\d+)\s+"([^"]+)"\)', raw):
        session, wnum, title = m.group(1), int(m.group(2)), m.group(3)
        if "Assembler" in title:
            ade_type = "assembler"
        elif "Explorer" in title:
            ade_type = "explorer"
        else:
            continue
        mode = "editing" if "Editing:" in title else "reading"
        modified = title.rstrip().endswith("*")
        results.append({
            "session": session,
            "window_num": wnum,
            "mode": mode,
            "modified": modified,
            "ade_type": ade_type,
            "title": title,
        })
    return results


def _close_background_sessions(client: VirtuosoClient) -> list[str]:
    """Close all non-GUI sessions (background + zombie). Returns closed session names.

    Zombie sessions (GUI-opened but window already closed) cannot be
    closed via maeCloseSession (ASSEMBLER-8051). We attempt to close
    them but silently ignore failures — they don't hold edit locks
    once their windows are gone.
    """
    r = client.execute_skill('maeGetSessions()')
    raw = (r.output or "").strip()
    if not raw or raw == "nil":
        return []

    sessions = re.findall(r'"([^"]+)"', raw)
    gui_sessions = {w["session"] for w in _get_session_windows(client)}
    closed = []
    for s in sessions:
        if s not in gui_sessions:
            client.execute_skill(f'errset(maeCloseSession(?session "{s}" ?forceClose t))')
            logger.info("Closed background/zombie session: %s", s)
            closed.append(s)
    return closed


# ---------------------------------------------------------------------------
# Background session (read/write config only)
# ---------------------------------------------------------------------------

def open_session(client: VirtuosoClient, lib: str, cell: str) -> str:
    """Open maestro in background via maeOpenSetup. Returns session string."""
    r = client.execute_skill(
        f'let((session) session = maeOpenSetup("{lib}" "{cell}" "maestro") '
        f'printf("[%s maeOpenSetup] %s/%s  session=%s\\n" nth(2 parseString(getCurrentTime())) "{lib}" "{cell}" session) '
        f'session)')
    session = (r.output or "").strip('"')
    if not session or session in ("nil", "t"):
        raise RuntimeError(f"maeOpenSetup failed for {lib}/{cell}")
    return session


def close_session(client: VirtuosoClient, session: str) -> None:
    """Close a background maestro session via maeCloseSession."""
    client.execute_skill(
        f'maeCloseSession(?session "{session}" ?forceClose t) '
        f'printf("[%s maeCloseSession] session=%s closed\\n" nth(2 parseString(getCurrentTime())) "{session}")')


def find_open_session(client: VirtuosoClient) -> str | None:
    """Find the first active session with a valid test. Returns session string or None."""
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


# ---------------------------------------------------------------------------
# GUI session (required for simulation)
# ---------------------------------------------------------------------------

def open_gui_session(client: VirtuosoClient, lib: str, cell: str) -> str:
    """Open maestro in GUI mode, ready for simulation. Returns session string.

    Handles all edge cases safely:
    1. Closes any background sessions (they hold lock files)
    2. If an Editing GUI session exists for this cell, reuses it
    3. If a Reading GUI session exists, closes it (discards changes)
    4. Opens fresh GUI + maeMakeEditable if needed

    Returns the session string (e.g. "fnxSession3").
    """
    # Step 1: close background sessions
    closed_bg = _close_background_sessions(client)
    if closed_bg:
        logger.info("Closed background sessions: %s", closed_bg)

    # Step 2: check existing GUI sessions
    windows = _get_session_windows(client)

    for w in windows:
        title = w["title"]
        is_target = (lib in title and cell in title)

        if is_target and w["mode"] == "editing":
            # Already editable for our cell — reuse
            logger.info("Reusing existing editable session: %s", w["session"])
            return w["session"]

        # Close windows that are:
        # - for a different cell (must release edit lock)
        # - for our cell but in reading mode
        logger.info("Closing session %s (%s, target=%s)", w["session"], w["mode"], is_target)
        close_gui_session(client, w["session"], save=(w["mode"] == "editing"))

    # Step 3: open in editable mode.
    # deOpenCellView with mode "a" opens editable. From a clean state
    # (no residual sessions), this opens Assembler by default.
    # Do NOT call maeOpenSetup afterwards — it creates a second
    # background session with its own lock, causing 8127 on next open.
    logger.info("Opening GUI (editable): %s/%s/maestro", lib, cell)
    r = client.execute_skill(
        f'deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "a")',
        timeout=10)
    if r.errors or not r.output or r.output.strip() in ("nil", ""):
        raise RuntimeError(f"deOpenCellView failed for {lib}/{cell}/maestro: {r.errors}")

    # Find the new session
    session = find_open_session(client)
    if not session:
        raise RuntimeError("No session found after opening GUI")
    logger.info("Opened GUI session: %s", session)
    return session


def close_gui_session(client: VirtuosoClient, session: str,
                      save: bool = True) -> None:
    """Close a GUI maestro session safely.

    Checks window state before closing:
    - Editing with changes: saves first (if save=True), then closes
    - Editing without changes: closes directly
    - Reading with changes + no other Editing session: promote to
      editable, save, then close
    - Reading with changes + another Editing session exists: close
      and discard changes (dismiss save dialog)
    - Reading without changes: closes directly

    Args:
        save: if True and session has unsaved changes, attempt to
              save before closing. If False, always discard changes.
    """
    windows = _get_session_windows(client)
    target_window = None
    for w in windows:
        if w["session"] == session:
            target_window = w
            break

    if target_window is None:
        # No GUI window — try background close
        logger.info("No GUI window for %s, trying maeCloseSession", session)
        client.execute_skill(f'maeCloseSession(?session "{session}" ?forceClose t)')
        return

    if target_window["modified"] and save:
        if target_window["mode"] == "editing":
            # Editing* — save, then close
            logger.info("Saving modified Editing session %s", session)
            client.execute_skill(f'maeSaveSetup(?session "{session}")')
        else:
            # Reading* — check if we can promote to editable
            other_editing = any(
                w["mode"] == "editing" and w["session"] != session
                for w in windows
            )
            if not other_editing:
                # No conflict — promote to editable, save, close
                logger.info("Promoting Reading* session %s to editable for save", session)
                r = client.execute_skill('maeMakeEditable()', timeout=10)
                if not r.errors:
                    client.execute_skill(f'maeSaveSetup(?session "{session}")')
                else:
                    logger.warning("maeMakeEditable failed, will discard changes: %s", r.errors)
            else:
                # Another session holds edit lock — must discard
                logger.info("Reading* session %s has conflicts, discarding changes", session)

    _close_gui_window(client, target_window, windows)

    # Purge cellview from memory to release internal edit lock.
    # Without this, deOpenCellView("a") on another cell may fail with
    # ASSEMBLER-8127 even after hiCloseWindow + maeCloseSession.
    _purge_maestro_cellviews(client)
    logger.info("Closed GUI session: %s", session)


def _close_gui_window(client: VirtuosoClient, window_info: dict,
                      all_windows: list[dict] | None = None) -> None:
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
    will_pop_dialog = window_info["modified"]

    dismiss_thread = None
    if will_pop_dialog:
        runner = client.ssh_runner
        if runner is not None:
            def _dismiss_save_dialog():
                """Send Alt+N after a short delay to dismiss save dialog."""
                _time.sleep(0.5)
                # Alt+N selects "No" (Don't Save) on save confirmation dialogs.
                # Escape only cancels the dialog without closing the window.
                _send_x11_alt_n(runner)

            dismiss_thread = threading.Thread(target=_dismiss_save_dialog, daemon=True)
            dismiss_thread.start()
            logger.info("Started dismiss thread for modified window %d", wnum)

    client.execute_skill(f'''
let((w)
  foreach(win hiGetWindowList()
    when(win~>windowNum == {wnum} w = win))
  when(w hiCloseWindow(w)))
''', timeout=15)

    if dismiss_thread is not None:
        dismiss_thread.join(timeout=10)

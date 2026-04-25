"""Test all Maestro GUI session lifecycle scenarios.

Covers:
  1. Clean open + close (no existing sessions)
  2. Reuse existing Editing session
  3. Close Reading session, reopen as Editing
  4. Handle background session cleanup
  5. Close Editing session with unsaved changes (save first)
  6. Close Reading session with unsaved changes (discard)

Usage:
    python examples/01_virtuoso/maestro/07_gui_session_lifecycle.py
"""

import sys
import time
import logging

from virtuoso_bridge import VirtuosoClient, decode_skill_output
from virtuoso_bridge.virtuoso.maestro.lifecycle import (
    open_gui_session,
    close_gui_session,
    open_session,
    close_session,
    _get_session_windows,
    _close_background_sessions,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

client = VirtuosoClient.from_env()
LIB = "PLAYGROUND_LLM"
CELL = "TB_SAMPLING_BTS_TOP_DIFF"

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS  {label}")
        passed += 1
    else:
        print(f"  FAIL  {label}  {detail}")
        failed += 1


def get_sessions():
    r = client.execute_skill('maeGetSessions()')
    raw = (r.output or "").strip()
    if not raw or raw == "nil":
        return []
    import re
    return re.findall(r'"([^"]+)"', raw)


def get_window_count():
    r = client.execute_skill('hiGetWindowList()')
    raw = (r.output or "").strip()
    return raw.count("window:") if raw else 0


def cleanup_all():
    """Force-close everything to get to clean state."""
    # Close all GUI windows with sessions
    windows = _get_session_windows(client)
    for w in windows:
        client.execute_skill(f'''
let((win)
  foreach(x hiGetWindowList()
    when(x~>windowNum == {w["window_num"]} win = x))
  when(win hiCloseWindow(win)))
''', timeout=10)
        time.sleep(0.5)
        # Dismiss any save dialog
        client.execute_skill('errset(hiFormDone(hiGetCurrentForm()))', timeout=5)

    # Close background sessions
    _close_background_sessions(client)

    # Verify clean
    time.sleep(0.5)


# =========================================================================
print("\n=== Setup: clean state ===")
cleanup_all()
sessions = get_sessions()
check("Clean state", len(sessions) == 0, f"sessions={sessions}")

# =========================================================================
print("\n=== Test 1: Clean open + close ===")
session = open_gui_session(client, LIB, CELL)
check("Open returns session", session is not None and session != "", f"got {session}")

windows = _get_session_windows(client)
check("One maestro window", len(windows) == 1, f"got {len(windows)}")
if windows:
    check("Mode is editing", windows[0]["mode"] == "editing")
    check("Not modified", not windows[0]["modified"])

close_gui_session(client, session)
sessions = get_sessions()
check("Session closed", len(sessions) == 0, f"sessions={sessions}")

# =========================================================================
print("\n=== Test 2: Reuse existing Editing session ===")
session1 = open_gui_session(client, LIB, CELL)
session2 = open_gui_session(client, LIB, CELL)
check("Same session reused", session1 == session2, f"{session1} vs {session2}")

windows = _get_session_windows(client)
check("Still one window", len(windows) == 1, f"got {len(windows)}")

close_gui_session(client, session1)

# =========================================================================
print("\n=== Test 3: Background session cleanup ===")
# Open background session (holds lock)
bg_session = open_session(client, LIB, CELL)
check("Background session opened", bg_session is not None)

# open_gui_session should clean it up automatically
gui_session = open_gui_session(client, LIB, CELL)
check("GUI session opened after bg cleanup", gui_session is not None)

# Background session should be gone
sessions = get_sessions()
check("Only GUI session remains", bg_session not in sessions,
      f"bg={bg_session} still in {sessions}")

close_gui_session(client, gui_session)

# =========================================================================
print("\n=== Test 4: Close Editing session with unsaved changes ===")
session = open_gui_session(client, LIB, CELL)

# Make a change to create the * (modified) state
client.execute_skill(f'maeSetVar("_vb_test_var" "999" ?session "{session}")')
time.sleep(0.5)

windows = _get_session_windows(client)
if windows:
    check("Modified flag set", windows[0]["modified"],
          f"title: {windows[0]['title']}")

# close_gui_session with save=True should save first, then close cleanly
close_gui_session(client, session, save=True)
sessions = get_sessions()
check("Session closed after save", len(sessions) == 0, f"sessions={sessions}")

# Clean up the test variable
temp = open_gui_session(client, LIB, CELL)
client.execute_skill(f'''
errset(axlRemoveElement(axlGetVar(axlGetMainSetupDB("{temp}") "_vb_test_var")))
''')
client.execute_skill(f'maeSaveSetup(?lib "{LIB}" ?cell "{CELL}" ?view "maestro" ?session "{temp}")')
close_gui_session(client, temp)

# =========================================================================
print("\n=== Test 5: Open when Reading session exists ===")
# Open in GUI but do NOT make editable -> Reading mode
client.execute_skill(
    f'deOpenCellView("{LIB}" "{CELL}" "maestro" "maestro" nil "r")')
time.sleep(0.5)

windows = _get_session_windows(client)
if windows:
    check("Reading mode initially", windows[0]["mode"] == "reading",
          f"got {windows[0]['mode']}")

# open_gui_session should close the reading session and reopen as editing
session = open_gui_session(client, LIB, CELL)
check("Converted to editing session", session is not None)

windows = _get_session_windows(client)
if windows:
    check("Now in editing mode", windows[0]["mode"] == "editing")

close_gui_session(client, session)

# =========================================================================
print("\n=== Test 6: Close Reading* with no edit conflict (promote+save) ===")
# Open as reading
client.execute_skill(
    f'deOpenCellView("{LIB}" "{CELL}" "maestro" "maestro" nil "r")')
time.sleep(0.5)

session = decode_skill_output(
    client.execute_skill('car(maeGetSessions())').output)

# Make a change -> Reading*
client.execute_skill(f'maeSetVar("_vb_test_var2" "888" ?session "{session}")')
time.sleep(0.5)

windows = _get_session_windows(client)
if windows:
    check("Reading* state", windows[0]["mode"] == "reading" and windows[0]["modified"],
          f"mode={windows[0]['mode']} modified={windows[0]['modified']}")

# close_gui_session should promote to editable, save, then close
close_gui_session(client, session, save=True)
sessions = get_sessions()
check("Session closed after promote+save", len(sessions) == 0, f"sessions={sessions}")

# Verify SKILL channel is alive (no dialog stuck)
r = client.execute_skill('1+1', timeout=5)
check("SKILL channel alive", r.output == "2", f"got {r.output}")

# Clean up test variable
temp = open_gui_session(client, LIB, CELL)
client.execute_skill(f'''
errset(axlRemoveElement(axlGetVar(axlGetMainSetupDB("{temp}") "_vb_test_var2")))
''')
client.execute_skill(f'maeSaveSetup(?lib "{LIB}" ?cell "{CELL}" ?view "maestro" ?session "{temp}")')
close_gui_session(client, temp)

# =========================================================================
print("\n=== Test 7: Close Reading* with edit conflict (defensive) ===")
# This should not happen in normal use, but code must handle it safely.
# Open one as editing (holds edit lock)
editing_session = open_gui_session(client, LIB, CELL)
check("Editing session opened", editing_session is not None)

# Open another as reading (second window — abnormal state)
client.execute_skill(
    f'deOpenCellView("{LIB}" "{CELL}" "maestro" "maestro" nil "r")')
time.sleep(0.5)

windows = _get_session_windows(client)
reading_windows = [w for w in windows if w["mode"] == "reading"]
check("Have reading window", len(reading_windows) == 1, f"got {len(reading_windows)}")

if reading_windows:
    reading_session = reading_windows[0]["session"]
    # Make a change in reading session -> Reading*
    client.execute_skill(f'maeSetVar("_vb_test_var3" "777" ?session "{reading_session}")')
    time.sleep(0.5)

    # close_gui_session should discard changes (can't promote, edit lock held)
    close_gui_session(client, reading_session, save=True)

    # Verify SKILL channel is alive (no dialog stuck)
    r = client.execute_skill('1+1', timeout=5)
    check("SKILL channel alive after discard", r.output == "2", f"got {r.output}")

    # Only editing session should remain
    sessions = get_sessions()
    check("Only editing session remains",
          len(sessions) == 1 and editing_session in sessions,
          f"sessions={sessions}")

# Clean up
close_gui_session(client, editing_session)

# =========================================================================
print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*60}")
sys.exit(1 if failed > 0 else 0)

"""Opt-in real window/exit tests using only fixture-owned CIWs and libraries."""

import json
import os
import sys
import time

import pytest

from tests.test_daemon_lifecycle_live import sessions
from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.virtuoso.ops import close_windows, request_exit


pytestmark = pytest.mark.skipif(
    os.getenv("VB_RUN_WINDOW_LIFECYCLE_TESTS") != "1" or sys.platform != "linux",
    reason="set VB_RUN_WINDOW_LIFECYCLE_TESTS=1 and VB_TEST_VIRTUOSO on a Cadence host",
)
LIB = "VB_WINDOW_LIFECYCLE"


def _library(target, reader, tmp_path):
    for item in (target, reader):
        assert item["evaluate"](f'ddCreateLib("{LIB}" {json.dumps(str(tmp_path / "library"))})') != "nil"


def _dirty_cell(target, name, *, window=True):
    evaluate = target["evaluate"]
    evaluate(
        f'vbCv = dbOpenCellViewByType("{LIB}" "{name}" "schematic" "schematic" "a") '
        'dbCreateProp(vbCv "vbMarker" "string" "saved") dbSave(vbCv) dbClose(vbCv)'
    )
    if window:
        # Only this fixture-owned CIW: allow the installed schematic license
        # fallback without the site's interactive "Next License" question.
        evaluate(
            'envSetVal("license" "VSEL_UseNextLicense" \'string "always")'
        )
        evaluate(
            f'{name}Window = geOpen(?lib "{LIB}" ?cell "{name}" ?view "schematic" '
            '?viewType "schematic" ?mode "a") '
            f'{name}Cv = {name}Window~>cellView'
        )
    else:
        evaluate(f'{name}Cv = dbOpenCellViewByType("{LIB}" "{name}" "schematic" "schematic" "a")')
    evaluate(f'{name}Cv~>vbMarker = "dirty"')


def _on_disk(reader, name):
    return reader["evaluate"](
        f'let((v) v = dbOpenCellViewByType("{LIB}" "{name}" "schematic" "schematic" "r") '
        'unwindProtect(v~>vbMarker dbClose(v)))'
    )


def _dirty_display(target):
    path = json.dumps(str(target["work"] / "dirty-display.drf"))
    assert target["evaluate"](f"drDumpDrf({path}) drLoadDrf({path})") == "t"


@pytest.mark.parametrize("policy", ["refuse", "save", "discard"])
def test_close_only_matching_windows_and_preserve_other_edits(sessions, tmp_path, policy):
    target = sessions("target", graphical=True)
    reader = sessions("reader")
    _library(target, reader, tmp_path)
    _dirty_cell(target, "selected")
    _dirty_cell(target, "keeper")
    result = target["client"].close_windows(LIB, "selected", "schematic", unsaved=policy)
    evaluate = target["evaluate"]
    if policy == "refuse":
        assert result.status == ExecutionStatus.ERROR
        assert evaluate("if(memq(selectedWindow hiGetWindowList()) t nil)") == "t"
        assert evaluate('selectedCv~>vbMarker') == '"dirty"'
    else:
        assert result.status == ExecutionStatus.SUCCESS, result
        assert evaluate("if(memq(selectedWindow hiGetWindowList()) t nil)") == "nil"
    assert evaluate("if(memq(keeperWindow hiGetWindowList()) t nil)") == "t"
    assert evaluate('keeperCv~>vbMarker') == '"dirty"'
    assert _on_disk(reader, "selected") == ('"dirty"' if policy == "save" else '"saved"')
    assert _on_disk(reader, "keeper") == '"saved"'


def test_save_that_leaves_dirty_state_refuses_to_close(sessions, tmp_path):
    target = sessions("target", graphical=True)
    reader = sessions("reader")
    _library(target, reader, tmp_path)
    _dirty_cell(target, "selected")
    target["evaluate"]("procedure(vbTestSaveWithoutSaving(cv) t)")
    skill = close_windows(LIB, "selected", unsaved="save").replace("dbSave(x)", "vbTestSaveWithoutSaving(x)")
    result = target["client"].execute_skill(skill, retry_connect=False)
    assert result.status == ExecutionStatus.ERROR
    assert target["evaluate"]("if(memq(selectedWindow hiGetWindowList()) t nil)") == "t"
    assert _on_disk(reader, "selected") == '"saved"'


@pytest.mark.parametrize("policy", ["refuse", "save", "discard"])
def test_exit_accounts_for_window_and_nonwindow_edits(sessions, tmp_path, policy):
    target = sessions("target", graphical=True)
    reader = sessions("reader")
    _library(target, reader, tmp_path)
    _dirty_cell(target, "selected")
    _dirty_cell(target, "hidden", window=False)
    _dirty_display(target)
    path = target["work"] / 'saved "display".drf'
    display = "save" if policy == "save" else "discard"
    result = target["client"].request_exit(
        unsaved=policy, display_resources=display,
        display_path=path if display == "save" else None,
    )
    if policy == "refuse":
        assert result.status == ExecutionStatus.ERROR
        assert target["evaluate"]("40+2") == "42"
        assert target["evaluate"]('hiddenCv~>vbMarker') == '"dirty"'
    else:
        assert result.status == ExecutionStatus.SUCCESS, result
        assert result.metadata["exit_state"] == "requested"
        target["proc"].wait(timeout=15)
    expected = '"dirty"' if policy == "save" else '"saved"'
    assert _on_disk(reader, "selected") == expected
    assert _on_disk(reader, "hidden") == expected
    assert reader["evaluate"]("40+2") == "42"
    assert path.exists() is (display == "save")
    if path.exists():
        assert path.stat().st_size > 0


def test_cancelled_exit_restores_display_prompt_setting(sessions):
    target = sessions("target", graphical=True)
    evaluate = target["evaluate"]
    _dirty_display(target)
    evaluate('envSetVal("layout" "drmSuppressSaveDialogBox" \'boolean nil)')
    evaluate("procedure(vbTestCancelExit() vbExitCancelled = t 'ignoreExit) regExitBefore('vbTestCancelExit)")
    result = target["client"].request_exit(display_resources="discard")
    assert result.status == ExecutionStatus.SUCCESS
    assert result.metadata["exit_state"] == "requested"
    deadline = time.monotonic() + 5
    while evaluate("boundp('vbExitCancelled) && vbExitCancelled") != "t":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert evaluate('envGetVal("layout" "drmSuppressSaveDialogBox")') == "nil"
    assert evaluate("40+2") == "42"
    evaluate("remExitProc('vbTestCancelExit)")
    target["client"].request_exit(display_resources="discard")
    target["proc"].wait(timeout=15)


def test_refuse_rechecks_edits_made_after_exit_was_queued(sessions, tmp_path):
    target = sessions("target", graphical=True)
    reader = sessions("reader")
    _library(target, reader, tmp_path)
    evaluate = target["evaluate"]
    evaluate("procedure(vbTestHoldExit(code delay) vbHeldExit = code t)")
    skill = request_exit(display_resources="discard").replace("hiRegTimer(", "vbTestHoldExit(")
    assert evaluate(skill) == '"exit_requested"'
    _dirty_cell(target, "hidden", window=False)
    result = target["client"].execute_skill("evalstring(vbHeldExit)", retry_connect=False)
    assert result.status == ExecutionStatus.ERROR
    assert evaluate('hiddenCv~>vbMarker') == '"dirty"'
    assert evaluate("40+2") == "42"
    assert _on_disk(reader, "hidden") == '"saved"'

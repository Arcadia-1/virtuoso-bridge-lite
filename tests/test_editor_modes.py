from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.layout import LayoutOps
from virtuoso_bridge.virtuoso.schematic import SchematicOps
from virtuoso_bridge.virtuoso.symbol import SymbolOps


def _open_command(editor: object) -> str:
    entered = editor.__enter__()  # type: ignore[attr-defined]
    assert entered is editor
    return editor.commands[0]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("ops_type", "view", "view_type"),
    [
        (SchematicOps, "schematic", "schematic"),
        (LayoutOps, "layout", "maskLayout"),
        (SymbolOps, "symbol", "schematicSymbol"),
    ],
)
def test_create_and_modify_make_overwrite_intent_explicit(
    ops_type: type[object],
    view: str,
    view_type: str,
) -> None:
    ops = ops_type(object())

    create_command = _open_command(ops.create("demoLib", "demoCell"))  # type: ignore[attr-defined]
    modify_command = _open_command(ops.modify("demoLib", "demoCell"))  # type: ignore[attr-defined]

    open_prefix = f'dbOpenCellViewByType("demoLib" "demoCell" "{view}" "{view_type}" '
    assert open_prefix + '"w")' in create_command
    assert open_prefix + '"a")' in modify_command


@pytest.mark.parametrize("ops_type", [SchematicOps, LayoutOps, SymbolOps])
def test_legacy_edit_is_deprecated_and_defaults_to_safe_append(ops_type: type[object]) -> None:
    ops = ops_type(object())

    with pytest.deprecated_call(match=r"use create\(\) or modify\(\) explicitly"):
        editor = ops.edit("demoLib", "demoCell")  # type: ignore[attr-defined]

    assert editor.mode == "a"  # type: ignore[attr-defined]


class _Recorder:
    def __init__(self):
        self.batches = []

    def execute_operations(self, commands, *, timeout=None, wrap_in_progn=True):
        from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult

        self.batches.append("\n".join(commands))
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")


@pytest.mark.parametrize("ops_type", [SchematicOps, LayoutOps, SymbolOps])
@pytest.mark.parametrize("method", ["create", "modify"])
def test_editors_close_the_cellview_they_opened_unless_kept_open(ops_type, method) -> None:
    from virtuoso_bridge.virtuoso.editor import close_editor_cellview

    for keep_open, closes in ((False, True), (True, False)):
        client = _Recorder()
        with getattr(ops_type(client), method)("demoLib", "demoCell", keep_open=keep_open):
            pass
        batch = client.batches[-1]
        assert (close_editor_cellview() in batch) is closes
        if closes:
            assert batch.index("dbSave(") < batch.index(close_editor_cellview())


def test_close_only_touches_a_saved_cellview_the_editor_opened() -> None:
    from virtuoso_bridge.virtuoso.editor import close_editor_cellview

    close = close_editor_cellview()
    guard = close[:close.index("dbClose(")]
    for condition in ("vbEditorOpenedCv", "dbValidP(vbEditorCv)", "!vbEditorCv~>modifiedButNotSaved"):
        assert condition in guard
    assert "dbClose(vbEditorCv)" in close


def test_layout_modify_does_not_claim_a_cellview_the_window_already_holds() -> None:
    client = _Recorder()
    with LayoutOps(client).modify("demoLib", "demoCell"):
        pass
    batch = client.batches[-1]
    # The window's cellview is reused as-is; only the open branch sets the flag.
    assert "rbEditCv = geGetEditCellView() vbEditorOpenedCv = nil " in batch
    assert "then rbEditCv else progn(vbEditorOpenedCv = t dbOpenCellViewByType(" in batch
    assert "vbEditorCv = cv vbEditorOpenedCv = vbEditorOpenedCv" in batch


def _failed(metadata):
    from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult

    return VirtuosoResult(status=ExecutionStatus.ERROR, errors=["request outcome unknown"], metadata=metadata)


def test_edit_failure_without_guard_metadata_stays_a_plain_runtime_error() -> None:
    from virtuoso_bridge.virtuoso.editor import ensure_operation_response

    with pytest.raises(RuntimeError) as caught:
        ensure_operation_response(_failed({}), context="schematic edit")
    assert type(caught.value) is RuntimeError
    assert str(caught.value) == "schematic edit failed: request outcome unknown"


def test_guarded_edit_failure_keeps_the_request_handle() -> None:
    from virtuoso_bridge.virtuoso.requests import RequestHandle, RequestRecoveryError

    handle = {"request_id": "a" * 32, "daemon_instance": "b" * 32, "virtuoso_pid": 4242}

    class _Client:
        def execute_operations(self, commands, *, timeout=None, wrap_in_progn=True):
            return _failed({"request_handle": handle, "outcome": "unknown"})

    with pytest.raises(RequestRecoveryError) as caught:
        with SchematicOps(_Client()).create("demoLib", "demoCell"):
            pass
    assert caught.value.handle == RequestHandle.model_validate(handle)
    assert str(caught.value) == "schematic edit failed: request outcome unknown"
    assert isinstance(caught.value, RuntimeError)


def test_dialog_blocked_edit_raises_dialog_blocked_error() -> None:
    from virtuoso_bridge.virtuoso.dialogs import DialogBlockedError
    from virtuoso_bridge.virtuoso.editor import ensure_operation_response

    with pytest.raises(DialogBlockedError) as caught:
        ensure_operation_response(
            _failed({"dialog_guard": {"status": "blocked"}, "outcome": "not_started"}),
            context="layout edit",
        )
    assert caught.value.outcome == "not_started"
    assert str(caught.value) == "layout edit failed: request outcome unknown"


@pytest.mark.parametrize("ops_type", [SchematicOps, LayoutOps, SymbolOps])
@pytest.mark.parametrize("keep_open", [False, True])
def test_editors_redraw_the_windows_of_the_edited_cellview(ops_type, keep_open) -> None:
    """Database edits do not repaint an open window; the editor redraws the
    windows that show its cellview after saving, and before closing it."""
    from virtuoso_bridge.virtuoso.editor import close_editor_cellview, redraw_editor_windows

    client = _Recorder()
    with ops_type(client).modify("demoLib", "demoCell", keep_open=keep_open):
        pass
    batch = client.batches[-1]
    redraw = redraw_editor_windows()
    assert batch.index("dbSave(") < batch.index(redraw)
    if not keep_open:
        assert batch.index(redraw) < batch.index(close_editor_cellview())
    # Only windows whose cellview is the editor's own are redrawn, and only if
    # the cellview handle is still valid; a GUI-less session has no windows.
    assert "isCallable('hiGetWindowList)" in redraw
    assert "dbValidP(vbEditorCv)" in redraw
    assert "vbWin~>cellView == vbEditorCv" in redraw
    # geGetWindowCellView would print a GE-2067 warning into the CIW for
    # every non-editor window (the CIW itself), checked on IC 23.1.
    assert "geGetWindowCellView" not in redraw
    assert "errset(hiRedraw(vbWin) nil)" in redraw


@pytest.mark.parametrize("ops_type", [SchematicOps, LayoutOps, SymbolOps])
@pytest.mark.parametrize("method", ["create", "modify"])
@pytest.mark.parametrize("keep_open", [False, True])
def test_editors_validate_open_save_vb_editor_cv_and_wrap_in_unwind_protect(
    ops_type,
    method: str,
    keep_open: bool,
) -> None:
    from tests.test_layout_edit_target import _balanced
    from virtuoso_bridge.virtuoso.editor import (
        close_editor_cellview,
        remember_editor_cellview,
        require_editor_cellview,
        save_editor_cellview,
    )

    client = _Recorder()
    user_cmd = 'cv = otherCv'
    with getattr(ops_type(client), method)('demo"Lib', "demoCell", keep_open=keep_open) as editor:
        editor.add(user_cmd)

    batch = client.batches[-1]
    assert _balanced(batch)
    assert batch.startswith("unwindProtect(progn(")
    view = "schematic" if ops_type is SchematicOps else ("layout" if ops_type is LayoutOps else "symbol")
    req = require_editor_cellview('demo"Lib', "demoCell", view)
    save = save_editor_cellview()
    assert req in batch
    assert 'unless(cv && dbValidP(cv) error("failed to open target cellview: demo\\"Lib/demoCell/' in req
    assert save in batch
    assert 'unless(dbSave(vbEditorCv) error("dbSave failed"))' in save
    assert "geGetEditCellView" not in save
    assert "dbSave(rbCv)" not in batch

    # Validate open before recording vbEditorCv / running user commands / saving.
    open_idx = batch.index("dbOpenCellViewByType(")
    req_idx = batch.index(req)
    rem_idx = batch.index(remember_editor_cellview(opened=(method == "create" or ops_type is not LayoutOps)))
    user_idx = batch.index(user_cmd)
    save_idx = batch.index(save)
    assert open_idx < req_idx < rem_idx < user_idx < save_idx

    # Cleanup runs inside unwindProtect after the body and always clears globals.
    cleanup = batch[batch.rindex(") progn(") :]
    assert "vbEditorCv = nil" in cleanup
    assert "vbEditorOpenedCv = nil" in cleanup
    if ops_type is LayoutOps:
        assert "vbLayoutEditCv = nil" in cleanup
    if not keep_open:
        assert close_editor_cellview() in cleanup
    else:
        assert close_editor_cellview() not in batch

    # Check helpers in schematic/symbol target vbEditorCv rather than a rebound cv.
    if ops_type is SchematicOps:
        assert "schCheck(vbEditorCv)" in batch
        assert user_idx < batch.index("schCheck(vbEditorCv)") < save_idx
    elif ops_type is SymbolOps:
        assert "rbCv = vbEditorCv" in batch
        assert user_idx < batch.index("rbCv = vbEditorCv") < save_idx

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

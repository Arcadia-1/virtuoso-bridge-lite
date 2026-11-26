from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.ops import (
    close_windows,
    default_view_type_for,
    open_cell_view,
    open_window,
)
from virtuoso_bridge.virtuoso.layout.ops import layout_read_summary


def test_default_view_type_for_common_cellviews() -> None:
    assert default_view_type_for("schematic") == "schematic"
    assert default_view_type_for("layout") == "maskLayout"
    assert default_view_type_for("layout1") == "maskLayout"
    assert default_view_type_for("symbol") == "schematicSymbol"
    assert default_view_type_for("maestro") == "maestro"
    assert default_view_type_for(" Symbol ") == "schematicSymbol"
    assert default_view_type_for("MAESTRO") == "maestro"
    assert default_view_type_for("customView") == "customView"


def test_open_window_uses_symbol_view_type_by_default() -> None:
    skill = open_window("demoLib", "nand2", view="symbol")

    assert '?view "symbol" ?viewType "schematicSymbol" ?mode "a"' in skill


def test_open_cell_view_uses_symbol_view_type_by_default() -> None:
    skill = open_cell_view("demoLib", "nand2", view="symbol", mode="r")

    assert skill == 'cv = dbOpenCellViewByType("demoLib" "nand2" "symbol" "schematicSymbol" "r")'


def test_open_cell_view_defaults_to_safe_append_mode() -> None:
    skill = open_cell_view("demoLib", "nand2", view="schematic")

    assert skill == 'cv = dbOpenCellViewByType("demoLib" "nand2" "schematic" "schematic" "a")'


def test_open_window_uses_maestro_view_type_by_default() -> None:
    skill = open_window("demoLib", "nand2", view="maestro")

    assert '?view "maestro" ?viewType "maestro" ?mode "a"' in skill


def test_open_cell_view_uses_maestro_view_type_by_default() -> None:
    skill = open_cell_view("demoLib", "nand2", view="maestro", mode="r")

    assert skill == 'cv = dbOpenCellViewByType("demoLib" "nand2" "maestro" "maestro" "r")'


def test_layout_read_summary_returns_after_instance_loop() -> None:
    skill = layout_read_summary("demoLib", "nand2")

    # Return the accumulated text even with zero instances, and only after
    # every instance has been appended.
    assert "return(unwindProtect(progn(" in skill
    assert "yCoord(inst~>xy)))) buf) when(ownsCv " in skill


def test_layout_reads_report_vias_as_their_own_objects() -> None:
    """Vias are neither shapes nor instances in OpenAccess (cv~>vias)."""
    from virtuoso_bridge.virtuoso.layout.ops import layout_read_geometry
    from virtuoso_bridge.virtuoso.layout.reader import parse_layout_geometry_output

    summary = layout_read_summary("demoLib", "top")
    assert "%d vias" in summary and "length(cv~>vias)" in summary
    assert "foreach(via cv~>vias" in summary and "via~>viaHeader~>viaDefName" in summary
    geometry = layout_read_geometry("demoLib", "top")
    assert "foreach(via cv~>vias" in geometry and "via~>origin" in geometry

    # As IC6.1.8 prints one M2_M1 via.
    parsed = parse_layout_geometry_output(
        '"via\\tobjType=stdVia\\tvia=M2_M1\\txy=(1.0 1.0)\\torient=\\"R0\\"\\tbbox=((0.81 0.81) (1.19 1.19))\\n"'
    )
    assert parsed == [{
        "kind": "via", "objType": "stdVia", "via": "M2_M1", "xy": (1.0, 1.0),
        "orient": "R0", "bbox": [(0.81, 0.81), (1.19, 1.19)],
    }]


@pytest.mark.parametrize(
    "raw",
    [
        '"ERROR: cannot open L/C/layout"',
        '"ERROR: cannot open L/C/layout\\n"',
        "ERROR: cannot open L/C/layout",
    ],
)
def test_parse_layout_geometry_output_raises_on_error_prefix(raw: str) -> None:
    from virtuoso_bridge.virtuoso.layout.reader import parse_layout_geometry_output

    with pytest.raises(ValueError, match="ERROR: cannot open L/C/layout"):
        parse_layout_geometry_output(raw)


def test_parse_layout_geometry_output_unquotes_and_unescapes_percent_l_fields() -> None:
    from virtuoso_bridge.virtuoso.layout.reader import parse_layout_geometry_output

    raw = (
        '"shape\\tobjType=label\\tlayer=M1\\tpurpose=drawing\\tbbox=((0 0) (1 1))\\tpoints=nil\\t'
        'xy=(0.5 0.5)\\torient=\\"R90\\"\\ttext=\\"line1\\\\nline2\\\\t\\\\\\"q\\\\\\"\\\\\\\\end\\"\\n'
        'mosaic\\tname=ARR\\tlib=\\"lib\\\\\\"1\\"\\tcell=\\"cell\\\\\\\\2\\"\\tview=\\"layout\\"\\t'
        'xy=(0 0)\\torient=\\"MY\\"\\trows=2\\tcolumns=3\\tbbox=((0 0) (4 6))\\n"'
    )
    parsed = parse_layout_geometry_output(raw)
    assert parsed[0]["orient"] == "R90"
    assert parsed[0]["text"] == 'line1\nline2\t"q"\\end'
    assert parsed[1]["lib"] == 'lib"1'
    assert parsed[1]["cell"] == "cell\\2"
    assert parsed[1]["view"] == "layout"
    assert parsed[1]["orient"] == "MY"


def test_escape_skill_string_escapes_newlines_and_carriage_returns() -> None:
    from virtuoso_bridge.virtuoso.ops import escape_skill_string, q

    escaped = escape_skill_string('line1\nline2\r\n"quoted"\\slash')
    assert "\n" not in escaped and "\r" not in escaped
    assert escaped == r'line1\nline2\r\n\"quoted\"\\slash'
    assert q("a\nb\rc") == r'"a\nb\rc"'


def test_open_window_shows_layout_hierarchy_only_in_a_window_it_opens() -> None:
    skill = open_window("demoLib", "nand2", view="layout")
    reuse, _, opened = skill.partition("else window = geOpen(")
    # Shift+F (full depth) instead of empty instance boxes; a reused window
    # belongs to whoever opened it and keeps its own display.
    assert "stopLevel" not in reuse and "leSetStopLevelToMaxHierDepth" not in reuse
    assert '~>cellViewType == "maskLayout"' in opened
    assert "leSetStopLevelToMaxHierDepth(window)" in opened
    assert "window~>stopLevel = 31" in opened
    assert "hiRedraw(window)" in opened
    assert "stopLevel" not in open_window("demoLib", "nand2", view="layout", show_hierarchy=False)


def _balanced(skill: str) -> bool:
    depth, in_string, escaped = 0, False, False
    for char in skill:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "()":
            depth += 1 if char == "(" else -1
            if depth < 0:
                return False
    return depth == 0 and not in_string


@pytest.mark.parametrize("unsaved", ["refuse", "save"])
def test_close_windows_never_leaves_a_modified_cellview_to_a_dialog(unsaved) -> None:
    skill = close_windows("demo\"Lib", "nand2", unsaved=unsaved)
    handled = skill.index("when(vbUnsaved ")
    # Unsaved cellviews are refused or saved before any window closes, so
    # closing cannot open the modal Save Changes dialog (checked on IC 23.1).
    assert "vbUnsaved = setof(x vbCvs dbIsCellViewModified(x))" in skill
    assert handled < skill.index("hiCloseWindow(")
    if unsaved == "refuse":
        assert "error(sprintf(nil \"unsaved cellviews would open a Save Changes dialog" in skill
        assert "dbSave(" not in skill
    else:
        assert "foreach(x vbUnsaved unless(dbSave(x) && !dbIsCellViewModified(x) error(" in skill
    # Matching by name, silently skipping non-editor windows such as the CIW,
    # and skipping a window an earlier close already took away.
    assert '(vbCv = w~>cellView) && vbCv~>libName == "demo\\"Lib" && vbCv~>cellName == "nand2")' in skill
    assert "vbCv~>viewName ==" not in skill and "geGetWindowCellView" not in skill
    assert "when(memq(w hiGetWindowList()) && car(errset(hiCloseWindow(w) nil)) " in skill
    assert skill.endswith("vbClosed)")
    assert _balanced(skill)


def test_close_windows_rejects_unknown_unsaved_policy() -> None:
    with pytest.raises(ValueError, match="refuse"):
        close_windows(unsaved="ignore")


def test_layout_bind_current_or_open_requires_writable_mode_for_edits() -> None:
    from virtuoso_bridge.virtuoso.layout.ops import layout_bind_current_or_open_cell_view

    for mode in ("a", "w"):
        skill = layout_bind_current_or_open_cell_view("demoLib", "nand2", mode=mode)
        assert _balanced(skill)
        assert 'member(rbEditCv~>mode list("a" "w"))' in skill

    read_skill = layout_bind_current_or_open_cell_view("demoLib", "nand2", mode="r")
    assert _balanced(read_skill)
    assert 'member(rbEditCv~>mode list("a" "w"))' not in read_skill


def test_layout_delete_cell_checks_dd_delete_obj_return_value() -> None:
    from virtuoso_bridge.virtuoso.layout.ops import layout_delete_cell

    skill = layout_delete_cell("demoLib", "nand2")
    assert _balanced(skill)
    assert (
        'if(ddcell then if(ddDeleteObj(ddcell) then sprintf(nil "deleted: %s/%s" "demoLib" "nand2") '
        'else sprintf(nil "ERROR: failed to delete: %s/%s" "demoLib" "nand2")) '
        'else sprintf(nil "ERROR: cell not found: %s/%s" "demoLib" "nand2"))'
    ) in skill

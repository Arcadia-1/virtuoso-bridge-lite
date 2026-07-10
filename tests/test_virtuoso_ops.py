from __future__ import annotations

from virtuoso_bridge.virtuoso.ops import (
    default_view_type_for,
    open_cell_view,
    open_window,
)


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


def test_open_window_uses_maestro_view_type_by_default() -> None:
    skill = open_window("demoLib", "nand2", view="maestro")

    assert '?view "maestro" ?viewType "maestro" ?mode "a"' in skill


def test_open_cell_view_uses_maestro_view_type_by_default() -> None:
    skill = open_cell_view("demoLib", "nand2", view="maestro", mode="r")

    assert skill == 'cv = dbOpenCellViewByType("demoLib" "nand2" "maestro" "maestro" "r")'

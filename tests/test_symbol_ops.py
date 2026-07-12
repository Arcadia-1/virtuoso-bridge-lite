from __future__ import annotations

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.symbol import SymbolOps
from virtuoso_bridge.virtuoso.symbol.editor import SymbolEditor
from virtuoso_bridge.virtuoso.symbol.ops import (
    symbol_check,
    symbol_create_ellipse,
    symbol_create_instance_label,
    symbol_create_label,
    symbol_create_line,
    symbol_create_logical_label,
    symbol_create_pin,
    symbol_create_pin_name,
    symbol_create_polygon,
    symbol_create_rect,
    symbol_create_selection_box,
    symbol_set_term_order,
)


def test_symbol_create_basic_shapes() -> None:
    assert (
        symbol_create_line("device", "drawing", [(0, 0), (1.5, 0)])
        == 'dbCreateLine(cv list("device" "drawing") \'((0.000 0.000) (1.500 0.000)))'
    )
    assert (
        symbol_create_rect("device", "drawing", -0.5, -0.25, 0.5, 0.25)
        == 'dbCreateRect(cv list("device" "drawing") list(list(-0.5 -0.25) list(0.5 0.25)))'
    )
    assert (
        symbol_create_polygon("device", "drawing", [(-1, 0), (0, 1), (1, 0)])
        == 'dbCreatePolygon(cv list("device" "drawing") \'((-1.000 0.000) (0.000 1.000) (1.000 0.000)))'
    )
    assert (
        symbol_create_ellipse("device", "drawing", -0.5, -0.5, 0.5, 0.5)
        == 'dbCreateEllipse(cv list("device" "drawing") list(list(-0.5 -0.5) list(0.5 0.5)))'
    )


def test_symbol_create_label_sets_optional_label_type_and_escapes_text() -> None:
    skill = symbol_create_label(
        "annotate",
        "drawing",
        1,
        2,
        '[@model:"x"]',
        "centerCenter",
        "R0",
        "stick",
        0.125,
        label_type="ILLabel",
    )

    assert (
        'rbLabel = dbCreateLabel(cv list("annotate" "drawing") \'(1.000 2.000) '
        '"[@model:\\"x\\"]" "centerCenter" "R0" "stick" 0.125)'
    ) in skill
    assert 'rbLabel~>labelType = "ILLabel"' in skill
    assert skill.endswith("rbLabel)")


def test_symbol_create_semantic_labels_use_native_label_choices() -> None:
    pin_name = symbol_create_pin_name("A", 1, 2)
    instance_name = symbol_create_instance_label(0, 1)
    logical_name = symbol_create_logical_label(0, -1)

    assert (
        'schCreateSymbolLabel(cv \'(1.000 2.000) "pin name" "A" '
        '"centerLeft" "R0" "stick" 0.0625 "normalLabel")'
    ) in pin_name
    assert 'error("semantic label not created: pin name")' in pin_name
    assert (
        'schCreateSymbolLabel(cv \'(0.000 1.000) "instance label" '
        '"[@instanceName]" "centerLeft" "R0" "stick" 0.0625 "NLPLabel")'
    ) in instance_name
    assert 'error("semantic label not created: instance label")' in instance_name
    assert (
        'schCreateSymbolLabel(cv \'(0.000 -1.000) "logical label" '
        '"[@partName]" "centerCenter" "R0" "stick" 0.0625 "NLPLabel")'
    ) in logical_name
    assert 'error("semantic label not created: logical label")' in logical_name


def test_symbol_create_semantic_labels_escape_custom_text_and_attributes() -> None:
    skill = symbol_create_instance_label(
        1,
        2,
        text='[@foo:"x"]',
        justification='center"Left',
        rotation="R90",
        font="fixed",
        height=0.125,
        cv_expr="symbolCv",
    )

    assert (
        'schCreateSymbolLabel(symbolCv \'(1.000 2.000) "instance label" '
        '"[@foo:\\"x\\"]" "center\\"Left" "R90" "fixed" 0.125 "NLPLabel")'
    ) in skill


def test_symbol_create_selection_box_uses_instance_drawing() -> None:
    skill = symbol_create_selection_box(-1, -0.5, 2, 0.5)

    assert (
        'dbCreateRect(cv list("instance" "drawing") '
        "list(list(-1 -0.5) list(2 0.5)))"
    ) in skill
    assert 'error("selection box not created")' in skill


def test_symbol_create_pin_creates_net_term_pin_rect_and_label() -> None:
    skill = symbol_create_pin(
        "A",
        1,
        2,
        direction="input",
        half_size=0.05,
        label=True,
        label_x=1.25,
        label_y=2,
    )

    assert 'rbExistingTerm = car(setof(x cv~>terminals x~>name == "A"))' in skill
    assert 'when(rbExistingTerm error("terminal already exists"))' in skill
    assert 'rbNet = car(setof(x cv~>nets x~>name == "A"))' in skill
    assert 'unless(rbNet rbNet = dbCreateNet(cv "A"))' in skill
    assert 'rbTerm = dbCreateTerm(rbNet "A" "input")' in skill
    assert (
        'rbRect = dbCreateRect(cv list("pin" "drawing") '
        "list(list(0.95 1.95) list(1.05 2.05)))"
    ) in skill
    assert 'rbPin = dbCreatePin(rbNet rbRect "A" rbTerm)' in skill
    assert (
        'schCreateSymbolLabel(cv \'(1.250 2.000) "pin name" "A" '
        '"centerLeft" "R0" "stick" 0.0625 "normalLabel")'
    ) in skill
    assert 'error("semantic label not created: pin name")' in skill
    assert skill.endswith("rbPin)")


def test_symbol_create_pin_without_label_still_creates_terminal_and_pin() -> None:
    skill = symbol_create_pin("A", 0, 0, label=False)

    assert "rbLabel" not in skill
    assert 'rbTerm = dbCreateTerm(rbNet "A" "inputOutput")' in skill
    assert (
        'rbRect = dbCreateRect(cv list("pin" "drawing") '
        "list(list(-0.0625 -0.0625) list(0.0625 0.0625)))"
    ) in skill
    assert 'rbPin = dbCreatePin(rbNet rbRect "A" rbTerm)' in skill


def test_symbol_create_pin_escapes_repeated_pin_name_and_direction() -> None:
    skill = symbol_create_pin(
        'A"\\B',
        0,
        0,
        direction='input"\\Output',
    )

    assert 'x~>name == "A\\"\\\\B"' in skill
    assert 'dbCreateNet(cv "A\\"\\\\B")' in skill
    assert 'dbCreateTerm(rbNet "A\\"\\\\B" "input\\"\\\\Output")' in skill
    assert 'dbCreatePin(rbNet rbRect "A\\"\\\\B" rbTerm)' in skill
    assert '"A\\"\\\\B" "centerLeft"' in skill


def test_symbol_set_term_order() -> None:
    assert symbol_set_term_order(["A", "Y", "VDD", "VSS"]) == 'cv~>termOrder = list("A" "Y" "VDD" "VSS")'
    assert symbol_set_term_order(['A"\\B', "Y"]) == 'cv~>termOrder = list("A\\"\\\\B" "Y")'


def test_symbol_check_uses_symbol_pin_list_api() -> None:
    skill = symbol_check(cv_expr="myCv")

    assert "rbCv = myCv" in skill
    assert "schSymbolToPinList(rbCv~>libName rbCv~>cellName rbCv~>viewName)" in skill
    assert 'unless(rbPinList error("symbol pin-list generation failed"))' in skill
    assert "schCheck(" not in skill
    assert skill.endswith("t)")


def test_symbol_editor_opens_symbol_view_and_saves() -> None:
    class Client:
        commands: list[str] | None = None
        timeout: int | None = None

        def execute_operations(self, commands: list[str], *, timeout: int):
            self.commands = commands
            self.timeout = timeout
            return {"ok": True, "result": {"status": "success"}}

    client = Client()
    with SymbolEditor(client, "demoLib", "nand2", timeout=7) as symbol:
        symbol.add(symbol_create_rect("device", "drawing", -1, -1, 1, 1))

    assert client.timeout == 7
    assert client.commands is not None
    assert client.commands[0] == 'cv = dbOpenCellViewByType("demoLib" "nand2" "symbol" "schematicSymbol" "w")'
    assert client.commands[1] == 'dbCreateRect(cv list("device" "drawing") list(list(-1 -1) list(1 1)))'
    assert client.commands[2] == symbol_check()
    assert "dbSave(rbCv)" in client.commands[3]


def test_symbol_editor_forwards_custom_view_type() -> None:
    class Client:
        commands: list[str] | None = None

        def execute_operations(self, commands: list[str], *, timeout: int):
            self.commands = commands
            return {"ok": True, "result": {"status": "success"}}

    client = Client()
    with SymbolEditor(client, "demoLib", "nand2", view_type="symbol") as symbol:
        symbol.add(symbol_create_rect("device", "drawing", -1, -1, 1, 1))

    assert client.commands is not None
    assert client.commands[0] == 'cv = dbOpenCellViewByType("demoLib" "nand2" "symbol" "symbol" "w")'


def test_symbol_editor_raises_for_failed_virtuoso_result() -> None:
    class Client:
        def execute_operations(self, commands: list[str], *, timeout: int):
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["symbol check failed"],
            )

    with pytest.raises(RuntimeError, match="symbol edit failed: symbol check failed"):
        with SymbolEditor(Client(), "demoLib", "nand2") as symbol:
            symbol.add(symbol_create_rect("device", "drawing", -1, -1, 1, 1))


def test_symbol_editor_raises_for_failed_dict_result_status() -> None:
    class Client:
        def execute_operations(self, commands: list[str], *, timeout: int):
            return {"ok": True, "result": {"status": "error", "errors": ["bad pin"]}}

    with pytest.raises(RuntimeError, match="symbol edit failed: bad pin"):
        with SymbolEditor(Client(), "demoLib", "nand2") as symbol:
            symbol.add(symbol_create_pin("A", 0, 0))


def test_virtuoso_client_exposes_symbol_ops() -> None:
    client = VirtuosoClient.local()

    assert isinstance(client.symbol, SymbolOps)

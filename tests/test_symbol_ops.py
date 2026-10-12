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


class _RecordingClient:
    def __init__(self, response: object = None) -> None:
        if response is None:
            response = {"ok": True, "result": {"status": "success"}}
        self.response = response
        self.commands: list[str] | None = None
        self.timeout: int | None = None

    def execute_operations(self, commands: list[str], *, timeout: int):
        self.commands = commands
        self.timeout = timeout
        return self.response


@pytest.mark.parametrize(
    ("builder", "coords", "db_function"),
    [
        (symbol_create_line, ([(0, 0), (1.5, 0)],), "dbCreateLine"),
        (symbol_create_rect, (-0.5, -0.25, 0.5, 0.25), "dbCreateRect"),
        (symbol_create_polygon, ([(-1, 0), (0, 1), (1, 0)],), "dbCreatePolygon"),
        (symbol_create_ellipse, (-0.5, -0.5, 0.5, 0.5), "dbCreateEllipse"),
    ],
)
def test_symbol_shape_builders_escape_layer_purpose(builder, coords, db_function: str) -> None:
    skill = builder('dev"ice', "draw\\ing", *coords, cv_expr="symCv")

    assert skill.startswith(f'{db_function}(symCv list("dev\\"ice" "draw\\\\ing") ')


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

    assert '"[@model:\\"x\\"]" "centerCenter" "R0" "stick" 0.125)' in skill
    assert 'rbLabel~>labelType = "ILLabel"' in skill


@pytest.mark.parametrize(
    ("builder", "args", "choice", "text", "label_type"),
    [
        (symbol_create_pin_name, ("A", 1, 2), "pin name", "A", "normalLabel"),
        (symbol_create_instance_label, (0, 1), "instance label", "[@instanceName]", "NLPLabel"),
        (symbol_create_logical_label, (0, -1), "logical label", "[@partName]", "NLPLabel"),
    ],
)
def test_symbol_semantic_labels_use_native_label_choices(
    builder,
    args: tuple,
    choice: str,
    text: str,
    label_type: str,
) -> None:
    skill = builder(*args)

    assert "schCreateSymbolLabel(cv " in skill
    assert f'"{choice}" "{text}"' in skill
    assert f'"{label_type}")' in skill
    assert f'error("semantic label not created: {choice}")' in skill


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

    assert 'dbCreateRect(cv list("instance" "drawing") list(list(-1 -0.5) list(2 0.5)))' in skill
    assert 'error("selection box not created")' in skill


def test_symbol_create_pin_rejects_existing_terminal_and_labels_natively() -> None:
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

    assert 'when(rbExistingTerm error("terminal already exists"))' in skill
    assert 'dbCreateTerm(rbNet "A" "input")' in skill
    assert 'list("pin" "drawing") list(list(0.95 1.95) list(1.05 2.05)))' in skill
    assert '\'(1.250 2.000) "pin name" "A"' in skill


def test_symbol_create_pin_without_label_still_creates_terminal_and_pin() -> None:
    skill = symbol_create_pin("A", 0, 0, label=False)

    assert "rbLabel" not in skill
    assert 'rbTerm = dbCreateTerm(rbNet "A" "inputOutput")' in skill
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

    assert "schSymbolToPinList(rbCv~>libName rbCv~>cellName rbCv~>viewName)" in skill
    assert 'unless(rbPinList error("symbol pin-list generation failed"))' in skill
    assert "schCheck(" not in skill


@pytest.mark.parametrize(
    ("kwargs", "open_command"),
    [
        ({}, 'cv = dbOpenCellViewByType("demoLib" "nand2" "symbol" "schematicSymbol" "a")'),
        ({"view_type": "symbol"}, 'cv = dbOpenCellViewByType("demoLib" "nand2" "symbol" "symbol" "a")'),
    ],
)
def test_symbol_editor_appends_then_checks_before_save(kwargs, open_command: str) -> None:
    client = _RecordingClient()
    shape = symbol_create_rect("device", "drawing", -1, -1, 1, 1)
    with SymbolEditor(client, "demoLib", "nand2", timeout=7, **kwargs) as symbol:
        symbol.add(shape)

    assert client.timeout == 7
    assert client.commands is not None
    assert client.commands[:3] == [open_command, shape, symbol_check()]
    assert "dbSave(rbCv)" in client.commands[3]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            VirtuosoResult(status=ExecutionStatus.ERROR, errors=["symbol check failed"]),
            "symbol edit failed: symbol check failed",
        ),
        (
            {"ok": True, "result": {"status": "error", "errors": ["bad pin"]}},
            "symbol edit failed: bad pin",
        ),
    ],
)
def test_symbol_editor_raises_for_failed_result(response: object, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        with SymbolEditor(_RecordingClient(response), "demoLib", "nand2") as symbol:
            symbol.add(symbol_create_pin("A", 0, 0))


def test_virtuoso_client_exposes_symbol_ops() -> None:
    client = VirtuosoClient.local()

    assert isinstance(client.symbol, SymbolOps)

from __future__ import annotations

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.response import response_fields
from virtuoso_bridge.virtuoso.symbol import SymbolOps
from virtuoso_bridge.virtuoso.symbol.reader import (
    parse_symbol_ports_output,
    read_symbol_ports,
    symbol_read_ports_skill,
)

_PORTS_OUTPUT = '(("term" "A" "input" 1 nil) ("label" "A" "" (0.0 0.0)) ("termOrder" ("A")))'


class _Client:
    def __init__(self, response: object) -> None:
        self.response = response
        self.skill: str | None = None
        self.timeout: int | None = None

    def execute_skill(self, skill: str, *, timeout: int):
        self.skill = skill
        self.timeout = timeout
        return self.response


def test_symbol_read_ports_skill_is_read_only_and_reports_documented_records() -> None:
    skill = symbol_read_ports_skill("demoLib", "nand2")

    assert '"symbol" "schematicSymbol" "r")' in skill
    assert 'label~>layerName == "instance" && label~>purpose == "drawing"' in skill
    assert 'list("pinOrder" schGetPinOrder(cv))' in skill
    assert 'closeFailures = cons("symbol close failed" closeFailures)' in skill


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (
            "term\tname=A\tdirection=input\tnumBits=1\tbbox=nil\ntermOrder\t(\"A\")",
            "structured SKILL list",
        ),
        (
            '("readFailed" "open symbol failed" ("symbol close failed"))',
            "symbol readback failed: open symbol failed; cleanup failed: symbol close failed",
        ),
        ('("readFailed" "open symbol failed")', "malformed symbol read failure output"),
        ('("readFailed" "open symbol failed" nil', "single complete SKILL list"),
        ('(("term" "A" "input" 1 nil)) ("unexpected")', "single complete SKILL list"),
    ],
)
def test_parse_symbol_ports_output_rejects_invalid_output(output: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_symbol_ports_output(output)


def test_parse_symbol_ports_output_preserves_label_delimiters_from_sexpr() -> None:
    parsed = parse_symbol_ports_output(
        r'(("label" "foo\tbar\nbaz\"\\end" "normalLabel" (0.2 0.0))'
        r' ("term" "A" "input" 1 ((0 0) (0.1 0.1)))'
        r' ("pinOrder" ("A" "Y"))'
        r' ("portOrder" ("Y" "A"))'
        r' ("termOrder" ("A" "Y")))'
    )

    assert parsed["labels"] == [
        {"text": 'foo\tbar\nbaz"\\end', "labelType": "normalLabel", "xy": [0.2, 0.0]}
    ]
    assert parsed["terms"] == [
        {"name": "A", "direction": "input", "numBits": 1, "bbox": [[0.0, 0.0], [0.1, 0.1]]}
    ]
    assert parsed["pinOrder"] == ["A", "Y"]
    assert parsed["portOrder"] == ["Y", "A"]
    assert parsed["termOrder"] == ["A", "Y"]


def test_parse_symbol_ports_output_reports_label_semantics_and_selection_box() -> None:
    parsed = parse_symbol_ports_output(
        '(("label" "[@instanceName]" "NLPLabel" (0 1) '
        '"instance" "label" "centerLeft" "R0" "stick" 0.0625 '
        '((-0.1 0.9) (0.8 1.1))) '
        '("selectionBox" ((-1 -0.5) (2 0.5))) '
        '("termOrder" ("A")))'
    )

    assert parsed["labels"] == [
        {
            "text": "[@instanceName]",
            "labelType": "NLPLabel",
            "xy": [0.0, 1.0],
            "layerName": "instance",
            "purpose": "label",
            "justify": "centerLeft",
            "orient": "R0",
            "font": "stick",
            "height": 0.0625,
            "bbox": [[-0.1, 0.9], [0.8, 1.1]],
        }
    ]
    assert parsed["selectionBoxes"] == [[[-1.0, -0.5], [2.0, 0.5]]]


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"view_type": "symbol"}],
)
def test_read_symbol_ports_executes_skill_and_forwards_options(kwargs) -> None:
    client = _Client(type("Result", (), {"output": _PORTS_OUTPUT})())

    parsed = read_symbol_ports(client, "demoLib", "nand2", timeout=17, **kwargs)

    assert parsed["terms"][0]["name"] == "A"
    assert parsed["termOrder"] == ["A"]
    assert client.timeout == 17
    assert client.skill == symbol_read_ports_skill("demoLib", "nand2", **kwargs)


@pytest.mark.parametrize(
    "response",
    [
        type("Result", (), {"output": _PORTS_OUTPUT})(),
        {"output": _PORTS_OUTPUT},
        {"ok": True, "result": {"status": "success", "output": _PORTS_OUTPUT}},
    ],
)
def test_symbol_ops_read_ports_accepts_response_shapes(response: object) -> None:
    parsed = SymbolOps(_Client(response)).read_ports("demoLib", "nand2")

    assert parsed["terms"][0]["name"] == "A"
    assert parsed["labels"][0]["text"] == "A"
    assert parsed["termOrder"] == ["A"]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            VirtuosoResult(status=ExecutionStatus.ERROR, errors=["open symbol failed"]),
            "read_symbol_ports SKILL error for demoLib/missing: open symbol failed",
        ),
        (
            {"ok": False, "error": "transport failed"},
            "read_symbol_ports SKILL error for demoLib/missing: transport failed",
        ),
        (
            VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output='("readFailed" "open symbol failed" ("symbol close failed"))',
            ),
            "read_symbol_ports failed for demoLib/missing: open symbol failed; "
            "cleanup failed: symbol close failed",
        ),
        (
            VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output='("readFailed" "open symbol failed" nil',
            ),
            "read_symbol_ports response error for demoLib/missing: "
            "symbol readback output must be a single complete SKILL list",
        ),
        (
            VirtuosoResult(status=ExecutionStatus.SUCCESS, output=""),
            "read_symbol_ports returned empty output",
        ),
    ],
)
def test_read_symbol_ports_raises_for_failed_reads(response: object, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        read_symbol_ports(_Client(response), "demoLib", "missing")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"errors": "transport failed"}, (["transport failed"], None, "")),
        ({"output": 17}, ([], None, "17")),
    ],
)
def test_response_fields_normalizes_scalar_values(response: dict, expected: tuple) -> None:
    assert response_fields(response) == expected

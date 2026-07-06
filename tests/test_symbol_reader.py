from __future__ import annotations

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.symbol import SymbolOps
from virtuoso_bridge.virtuoso.symbol.reader import (
    parse_symbol_ports_output,
    read_symbol_ports,
    symbol_read_ports_skill,
)


def test_symbol_read_ports_skill_opens_symbol_and_reports_terms_labels_and_order() -> None:
    skill = symbol_read_ports_skill("demoLib", "nand2")

    assert 'dbOpenCellViewByType("demoLib" "nand2" "symbol" "schematicSymbol" "r")' in skill
    assert 'result = cons(list("term"' in skill
    assert "fig = car(errset(pin~>fig nil))" in skill
    assert "unless(fig fig = car(errset(car(pin~>figs) nil)))" in skill
    assert "bbox = list(list(xCoord(car(fig~>bBox)) yCoord(car(fig~>bBox)))" in skill
    assert "list(xCoord(cadr(fig~>bBox)) yCoord(cadr(fig~>bBox))))" in skill
    assert 'result = cons(list("label"' in skill
    assert "xy = list(xCoord(label~>xy) yCoord(label~>xy))" in skill
    assert 'result = cons(list("termOrder" cv~>termOrder) result)' in skill
    assert "dbClose(cv)" in skill
    assert skill.endswith("reverse(result))")


def test_parse_symbol_ports_output_rejects_legacy_tsv() -> None:
    output = "term\tname=A\tdirection=input\tnumBits=1\tbbox=nil\ntermOrder\t(\"A\")"

    with pytest.raises(ValueError, match="structured SKILL list"):
        parse_symbol_ports_output(output)


def test_parse_symbol_ports_output_preserves_label_delimiters_from_sexpr() -> None:
    parsed = parse_symbol_ports_output(
        r'(("label" "foo\tbar\nbaz\"\\end" "normalLabel" (0.2 0.0))'
        r' ("term" "A" "input" 1 ((0 0) (0.1 0.1)))'
        r' ("termOrder" ("A")))'
    )

    assert parsed["labels"] == [
        {"text": 'foo\tbar\nbaz"\\end', "labelType": "normalLabel", "xy": [0.2, 0.0]}
    ]
    assert parsed["terms"] == [
        {"name": "A", "direction": "input", "numBits": 1, "bbox": [[0.0, 0.0], [0.1, 0.1]]}
    ]
    assert parsed["termOrder"] == ["A"]


def test_read_symbol_ports_executes_skill() -> None:
    class Client:
        skill: str | None = None
        timeout: int | None = None

        def execute_skill(self, skill: str, *, timeout: int):
            self.skill = skill
            self.timeout = timeout
            return type("Result", (), {"output": '(("term" "A" "input" 1 nil) ("termOrder" ("A")))'})()

    client = Client()
    parsed = read_symbol_ports(client, "demoLib", "nand2", timeout=17)

    assert parsed["terms"][0]["name"] == "A"
    assert parsed["termOrder"] == ["A"]
    assert client.timeout == 17
    assert client.skill is not None
    assert 'dbOpenCellViewByType("demoLib" "nand2" "symbol" "schematicSymbol" "r")' in client.skill


def test_read_symbol_ports_forwards_custom_view_type() -> None:
    class Client:
        skill: str | None = None

        def execute_skill(self, skill: str, *, timeout: int):
            self.skill = skill
            return type("Result", (), {"output": '(("termOrder" ("A")))'})()

    client = Client()
    read_symbol_ports(client, "demoLib", "nand2", view_type="symbol")

    assert client.skill is not None
    assert 'dbOpenCellViewByType("demoLib" "nand2" "symbol" "symbol" "r")' in client.skill


def test_read_symbol_ports_raises_on_skill_error() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["open symbol failed"],
            )

    with pytest.raises(RuntimeError, match="read_symbol_ports SKILL error: open symbol failed"):
        read_symbol_ports(Client(), "demoLib", "missing")


def test_read_symbol_ports_raises_on_empty_output() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="")

    with pytest.raises(RuntimeError, match="read_symbol_ports returned empty output"):
        read_symbol_ports(Client(), "demoLib", "missing")


def test_read_symbol_ports_raises_on_dict_transport_error() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return {"ok": False, "error": "transport failed"}

    with pytest.raises(RuntimeError, match="read_symbol_ports SKILL error: transport failed"):
        read_symbol_ports(Client(), "demoLib", "missing")


def test_read_symbol_ports_parses_structured_skill_output() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return type(
                "Result",
                (),
                {
                    "output": (
                        '((\"term\" \"A\" \"input\" 1 nil) '
                        '(\"label\" \"A\" \"\" (0.0 0.0)) '
                        '(\"termOrder\" (\"A\")))'
                    )
                },
            )()

    parsed = read_symbol_ports(Client(), "demoLib", "nand2")

    assert parsed["terms"][0]["name"] == "A"
    assert parsed["labels"][0]["text"] == "A"
    assert parsed["termOrder"] == ["A"]


def test_symbol_ops_exposes_read_ports() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return {"output": '(("term" "A" "input" 1 nil) ("termOrder" ("A")))'}

    ops = SymbolOps(Client())

    assert ops.read_ports("demoLib", "nand2")["termOrder"] == ["A"]


def test_read_symbol_ports_accepts_nested_dict_output() -> None:
    class Client:
        def execute_skill(self, skill: str, *, timeout: int):
            return {
                "ok": True,
                "result": {
                    "status": "success",
                    "output": '(("term" "A" "input" 1 nil) ("termOrder" ("A")))',
                },
            }

    parsed = read_symbol_ports(Client(), "demoLib", "nand2")

    assert parsed["terms"][0]["name"] == "A"
    assert parsed["termOrder"] == ["A"]

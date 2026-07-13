import ast
from pathlib import Path

import pytest

from analog_opt.units import UnitError, format_quantity, parse_quantity


@pytest.mark.parametrize(
    ("text", "dimension", "expected"),
    [
        ("10uA", "current", 10e-6),
        ("3.3V", "voltage", 3.3),
        ("2pF", "capacitance", 2e-12),
        ("10kOhm", "resistance", 10e3),
    ],
)
def test_parse_required_quantities(text, dimension, expected):
    assert parse_quantity(text, dimension) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("text", "dimension", "expected"),
    [
        ("2.5", "scalar", 2.5),
        ("1V", "voltage", 1.0),
        ("1mV", "voltage", 1e-3),
        ("1uV", "voltage", 1e-6),
        ("1A", "current", 1.0),
        ("1mA", "current", 1e-3),
        ("1uA", "current", 1e-6),
        ("1nA", "current", 1e-9),
        ("1F", "capacitance", 1.0),
        ("1nF", "capacitance", 1e-9),
        ("1pF", "capacitance", 1e-12),
        ("1Ohm", "resistance", 1.0),
        ("1kOhm", "resistance", 1e3),
        ("1MOhm", "resistance", 1e6),
        ("1Hz", "frequency", 1.0),
        ("1kHz", "frequency", 1e3),
        ("1MHz", "frequency", 1e6),
        ("1GHz", "frequency", 1e9),
        ("1s", "time", 1.0),
        ("1ms", "time", 1e-3),
        ("1us", "time", 1e-6),
        ("1ns", "time", 1e-9),
        ("1m", "length", 1.0),
        ("1mm", "length", 1e-3),
        ("1um", "length", 1e-6),
        ("1nm", "length", 1e-9),
        ("1W", "power", 1.0),
        ("1mW", "power", 1e-3),
        ("1uW", "power", 1e-6),
    ],
)
def test_parse_supported_units(text, dimension, expected):
    assert parse_quantity(text, dimension) == pytest.approx(expected)


def test_parse_rejects_dimension_mismatch():
    with pytest.raises(UnitError):
        parse_quantity("10uA", "voltage")


@pytest.mark.parametrize("text", ["1kV", "1foo", "1", "not-a-number"])
def test_parse_rejects_unknown_or_missing_units_for_voltage(text):
    with pytest.raises(UnitError):
        parse_quantity(text, "voltage")


def test_scalar_rejects_units():
    with pytest.raises(UnitError):
        parse_quantity("2V", "scalar")


def test_unknown_dimension_is_rejected():
    with pytest.raises(UnitError):
        parse_quantity("1V", "temperature")


def test_format_quantity():
    assert format_quantity(10e-6, "uA") == "10uA"


def test_format_rejects_unknown_unit():
    with pytest.raises(UnitError):
        format_quantity(1.0, "foo")

def test_units_module_uses_python_39_compatible_annotations():
    units_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "smic180-simulator"
        / "analog_opt"
        / "units.py"
    )
    module = ast.parse(units_path.read_text(encoding="utf-8-sig"))
    annotations = [
        node.annotation
        for node in ast.walk(module)
        if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation is not None
    ]
    assert not any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
        for annotation in annotations
        for node in ast.walk(annotation)
    )


def test_parse_huge_scalar_raises_unit_error():
    with pytest.raises(UnitError):
        parse_quantity(10**10000, "scalar")


def test_format_huge_quantity_raises_unit_error():
    with pytest.raises(UnitError):
        format_quantity(10**10000, "V")

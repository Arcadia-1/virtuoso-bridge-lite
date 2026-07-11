"""Strict parsing and formatting for analog optimization quantities."""

from __future__ import annotations

import math
import re
from numbers import Real


class UnitError(ValueError):
    """Raised when a quantity has an invalid unit or dimension."""


_DIMENSION_UNITS = {
    "scalar": {"": 1.0},
    "voltage": {"V": 1.0, "mV": 1e-3, "uV": 1e-6},
    "current": {"A": 1.0, "mA": 1e-3, "uA": 1e-6, "nA": 1e-9},
    "capacitance": {"F": 1.0, "nF": 1e-9, "pF": 1e-12},
    "resistance": {"Ohm": 1.0, "kOhm": 1e3, "MOhm": 1e6},
    "frequency": {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9},
    "time": {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9},
    "length": {"m": 1.0, "mm": 1e-3, "um": 1e-6, "nm": 1e-9},
    "power": {"W": 1.0, "mW": 1e-3, "uW": 1e-6},
}

_UNIT_FACTORS = {
    unit: factor
    for units in _DIMENSION_UNITS.values()
    for unit, factor in units.items()
    if unit
}

_QUANTITY_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z]*)\s*$"
)


def parse_quantity(value: str | Real, dimension: str) -> float:
    """Parse a quantity into its SI scalar value for the expected dimension."""
    try:
        valid_units = _DIMENSION_UNITS[dimension]
    except KeyError as exc:
        raise UnitError(f"unknown dimension: {dimension}") from exc

    if isinstance(value, Real) and not isinstance(value, bool):
        if dimension != "scalar":
            raise UnitError(f"unit required for dimension: {dimension}")
        result = float(value)
    elif isinstance(value, str):
        match = _QUANTITY_RE.fullmatch(value)
        if match is None:
            raise UnitError(f"invalid quantity: {value!r}")
        number_text, unit = match.groups()
        if unit not in _UNIT_FACTORS and unit != "":
            raise UnitError(f"unknown unit: {unit}")
        if unit not in valid_units:
            raise UnitError(f"unit {unit or '<none>'} does not match {dimension}")
        result = float(number_text) * valid_units[unit]
    else:
        raise UnitError(f"unsupported quantity type: {type(value).__name__}")

    if not math.isfinite(result):
        raise UnitError("quantity must be finite")
    return result


def format_quantity(value: Real, unit: str) -> str:
    """Format an SI scalar value using a supported unit."""
    try:
        factor = _UNIT_FACTORS[unit]
    except KeyError as exc:
        raise UnitError(f"unknown unit: {unit}") from exc
    if not isinstance(value, Real) or isinstance(value, bool):
        raise UnitError(f"unsupported quantity type: {type(value).__name__}")

    scaled = float(value) / factor
    if not math.isfinite(scaled):
        raise UnitError("quantity must be finite")
    return f"{scaled:.12g}{unit}"

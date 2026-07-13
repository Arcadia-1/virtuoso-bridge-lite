"""Strict SI quantity parsing used by the analog design schemas."""

from __future__ import annotations

import math
import re


class UnitError(ValueError):
    """Raised for invalid or dimensionally incompatible quantities."""


_PREFIXES = {"": 1.0, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_UNITS = {
    "voltage": "V",
    "current": "A",
    "capacitance": "F",
    "frequency": "Hz",
    "resistance": "Ohm",
    "power": "W",
    "time": "s",
    "length": "m",
    "temperature": "C",
    "gain_db": "dB",
    "angle": "deg",
}
_PATTERN = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([fpnumkKMG]?)([A-Za-z]+)\s*$")
_SLEW_PATTERN = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([fpnumkKMG]?)V\s*/\s*([fpnumkKMG]?)s\s*$")


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise UnitError("quantity must be finite")
    return value


def parse_quantity(value: object, dimension: str) -> float:
    if isinstance(value, bool):
        raise UnitError("boolean is not a quantity")
    if isinstance(value, (int, float)):
        return _finite(float(value))
    if not isinstance(value, str):
        raise UnitError("quantity must be a number or SI string")
    if dimension == "slew_rate":
        match = _SLEW_PATTERN.match(value)
        if not match:
            raise UnitError(f"invalid slew-rate quantity: {value!r}")
        magnitude, numerator_prefix, denominator_prefix = match.groups()
        return _finite(float(magnitude) * _PREFIXES[numerator_prefix] / _PREFIXES[denominator_prefix])
    expected = _UNITS.get(dimension)
    if expected is None:
        raise UnitError(f"unsupported quantity dimension: {dimension}")
    match = _PATTERN.match(value)
    if not match:
        raise UnitError(f"invalid {dimension} quantity: {value!r}")
    magnitude, prefix, unit = match.groups()
    if unit != expected:
        raise UnitError(f"expected {expected} for {dimension}, got {unit}")
    if dimension in {"temperature", "gain_db", "angle"} and prefix:
        raise UnitError(f"prefix is not allowed for {dimension}")
    return _finite(float(magnitude) * _PREFIXES[prefix])

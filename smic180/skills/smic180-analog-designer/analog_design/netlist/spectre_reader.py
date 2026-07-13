"""Constrained Spectre circuit parser for equivalence checks."""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Mapping


class NetlistParseError(ValueError):
    """Raised when a circuit netlist is outside the supported flat subset."""


_PREFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_NUMBER = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([fpnumkKMG]?)$")
_INSTANCE = re.compile(r"^(\S+)\s+\(([^)]*)\)\s+(\S+)(?:\s+(.*))?$")


@dataclass(frozen=True)
class ParsedInstance:
    name: str
    nodes: tuple[str, ...]
    model: str
    parameters: Mapping[str, float | str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class ParsedCircuit:
    name: str
    ports: tuple[str, ...]
    instances: Mapping[str, ParsedInstance]

    def __post_init__(self) -> None:
        object.__setattr__(self, "instances", MappingProxyType(dict(self.instances)))


def _numeric_atom(token: str) -> float | None:
    candidate = token.strip()
    while candidate.startswith("(") and candidate.endswith(")"):
        candidate = candidate[1:-1].strip()
    match = _NUMBER.match(candidate)
    if not match:
        return None
    magnitude, prefix = match.groups()
    return float(magnitude) * (_PREFIX[prefix] if prefix else 1.0)


def _value(token: str) -> float | str:
    factors = token.split("*")
    parsed = [_numeric_atom(factor) for factor in factors]
    if all(value is not None for value in parsed):
        result = 1.0
        for value in parsed:
            result *= float(value)
        return result
    return token


def _logical_lines(text: str) -> tuple[str, ...]:
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if pending:
            stripped = f"{pending} {stripped}"
        if stripped.endswith("\\"):
            pending = stripped[:-1].rstrip()
            continue
        if stripped:
            lines.append(stripped)
        pending = ""
    if pending:
        raise NetlistParseError("unterminated line continuation")
    return tuple(lines)


def parse_spectre_circuit(
    text: str,
    *,
    flat_name: str | None = None,
    flat_ports: tuple[str, ...] = (),
) -> ParsedCircuit:
    logical_lines = _logical_lines(text)
    has_subcircuit = any(line.startswith("subckt ") for line in logical_lines)
    if not has_subcircuit and flat_name is None:
        raise NetlistParseError("netlist contains no supported subcircuit")

    name = flat_name if not has_subcircuit else None
    ports = tuple(flat_ports) if not has_subcircuit else ()
    instances: dict[str, ParsedInstance] = {}
    inside = not has_subcircuit
    for line in logical_lines:
        if line.startswith("//") or line.startswith("simulator ") or line.startswith("include ") or line.startswith("global "):
            continue
        if line.startswith("subckt "):
            if inside:
                raise NetlistParseError("nested subcircuits are unsupported")
            tokens = line.split()
            if len(tokens) < 2:
                raise NetlistParseError("invalid subckt declaration")
            name = tokens[1]
            ports = tuple(tokens[2:])
            inside = True
            continue
        if line.startswith("ends"):
            inside = False
            break
        if not inside:
            continue
        match = _INSTANCE.match(line)
        if not match:
            raise NetlistParseError(f"unsupported circuit line: {line}")
        instance_name, node_text, model, parameter_text = match.groups()
        if instance_name in instances:
            raise NetlistParseError(f"duplicate instance: {instance_name}")
        parameters: dict[str, float | str] = {}
        if parameter_text:
            for token in parameter_text.split():
                if "=" not in token:
                    raise NetlistParseError(f"unsupported instance token: {token}")
                key, value = token.split("=", 1)
                parameters[key] = _value(value)
        instances[instance_name] = ParsedInstance(instance_name, tuple(node_text.split()), model, parameters)
    if name is None:
        raise NetlistParseError("netlist contains no supported subcircuit")
    return ParsedCircuit(name, ports, instances)
"""Version-1 Circuit IR records and structural loading."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .jsonio import StrictJsonError, load_strict_json
from .units import UnitError, parse_quantity


class IrError(ValueError):
    """Raised when Circuit IR is structurally invalid."""


_REQUIRED = (
    "version", "metadata", "technology", "circuit", "ports", "nets",
    "instances", "parameters", "matching_groups", "supplies", "biases",
    "analyses", "measurements", "constraints", "optimization", "provenance",
)
_PARAMETER_DIMENSIONS = {"length", "voltage", "current", "capacitance", "resistance", "frequency", "power", "time", "slew_rate"}


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IrError(f"{label} must be an object")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise IrError(f"{label} must be an array")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or not value.replace("_", "a").isalnum():
        raise IrError(f"{label} must be a stable identifier")
    return value


def _unique(records: list[Any], label: str) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records):
        item = _mapping(record, f"{label}[{index}]")
        identity = _identifier(item.get("id"), f"{label}[{index}].id")
        if identity in seen:
            singular = label[:-1] if label.endswith("s") else label
            raise IrError(f"duplicate {singular} id: {identity}")
        seen.add(identity)


def _frozen_mapping(value: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class Port:
    id: str
    direction: str
    kind: str


@dataclass(frozen=True)
class Net:
    id: str
    critical: bool


@dataclass(frozen=True)
class Instance:
    id: str
    role: str
    device_class: str
    master_ref: str
    terminals: Mapping[str, str]
    logical_parameters: Mapping[str, Any]
    physical_parameters: Mapping[str, Any]
    cdf_expectations: Mapping[str, Any]
    optimization_refs: tuple[str, ...]
    matching_groups: tuple[str, ...]
    rationale: tuple[str, ...]


@dataclass(frozen=True)
class Parameter:
    id: str
    dimension: str
    value: float
    minimum: float
    maximum: float
    target: str
    linked_instances: tuple[str, ...]
    quantization: Any
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class MatchingGroup:
    id: str
    instances: tuple[str, ...]
    parameters: tuple[str, ...]


@dataclass(frozen=True)
class CircuitIr:
    version: int
    metadata: Mapping[str, Any]
    technology: Mapping[str, Any]
    circuit: Mapping[str, Any]
    ports: tuple[Port, ...]
    nets: tuple[Net, ...]
    instances: tuple[Instance, ...]
    parameters: tuple[Parameter, ...]
    matching_groups: tuple[MatchingGroup, ...]
    supplies: tuple[Mapping[str, Any], ...]
    biases: tuple[Mapping[str, Any], ...]
    analyses: tuple[Mapping[str, Any], ...]
    measurements: tuple[Mapping[str, Any], ...]
    constraints: tuple[Mapping[str, Any], ...]
    optimization: Mapping[str, Any]
    provenance: Mapping[str, Any]
    source_data: Mapping[str, Any]

    def instance(self, identity: str) -> Instance:
        for item in self.instances:
            if item.id == identity:
                return item
        raise KeyError(identity)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def canonical_ir_digest(data: Mapping[str, Any]) -> str:
    payload = json.dumps(_json_value(data), allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _instance(record: object, index: int, net_ids: set[str]) -> Instance:
    item = _mapping(record, f"instances[{index}]")
    identity = _identifier(item.get("id"), f"instances[{index}].id")
    terminals = _mapping(item.get("terminals"), f"instance {identity} terminals")
    if not terminals:
        raise IrError(f"instance {identity} terminals must not be empty")
    normalized_terminals: dict[str, str] = {}
    for terminal, net in terminals.items():
        terminal_id = _identifier(terminal, f"instance {identity} terminal")
        net_id = _identifier(net, f"instance {identity} terminal net")
        if net_id not in net_ids:
            raise IrError(f"instance {identity} references unknown net {net_id}")
        normalized_terminals[terminal_id] = net_id
    return Instance(
        id=identity,
        role=_identifier(item.get("role"), f"instance {identity} role"),
        device_class=_identifier(str(item.get("device_class", "")).replace(".", "_"), f"instance {identity} device_class").replace("_", ".", 1),
        master_ref=str(item.get("master_ref", "")),
        terminals=_frozen_mapping(normalized_terminals),
        logical_parameters=_frozen_mapping(_mapping(item.get("logical_parameters"), f"instance {identity} logical_parameters")),
        physical_parameters=_frozen_mapping(_mapping(item.get("physical_parameters"), f"instance {identity} physical_parameters")),
        cdf_expectations=_frozen_mapping(_mapping(item.get("cdf_expectations"), f"instance {identity} cdf_expectations")),
        optimization_refs=tuple(_list(item.get("optimization_refs"), f"instance {identity} optimization_refs")),
        matching_groups=tuple(_list(item.get("matching_groups"), f"instance {identity} matching_groups")),
        rationale=tuple(_list(item.get("rationale"), f"instance {identity} rationale")),
    )


def _parameter(record: object, index: int) -> Parameter:
    item = _mapping(record, f"parameters[{index}]")
    identity = _identifier(item.get("id"), f"parameters[{index}].id")
    dimension = item.get("dimension")
    if dimension not in _PARAMETER_DIMENSIONS:
        raise IrError(f"parameter {identity} dimension is invalid")
    bounds = _mapping(item.get("bounds"), f"parameter {identity} bounds")
    try:
        value = parse_quantity(item.get("value"), dimension)
        minimum = parse_quantity(bounds.get("minimum"), dimension)
        maximum = parse_quantity(bounds.get("maximum"), dimension)
    except UnitError as exc:
        raise IrError(f"parameter {identity} bounds/value are invalid: {exc}") from exc
    if minimum > maximum or not minimum <= value <= maximum:
        raise IrError(f"parameter {identity} bounds must be ordered and contain value")
    return Parameter(
        id=identity,
        dimension=dimension,
        value=value,
        minimum=minimum,
        maximum=maximum,
        target=_identifier(item.get("target"), f"parameter {identity} target"),
        linked_instances=tuple(_list(item.get("linked_instances"), f"parameter {identity} linked_instances")),
        quantization=item.get("quantization"),
        provenance=_frozen_mapping(_mapping(item.get("provenance"), f"parameter {identity} provenance")),
    )


def circuit_ir_from_data(data: object) -> CircuitIr:
    if not isinstance(data, dict):
        raise IrError("Circuit IR must be an object")
    for field in _REQUIRED:
        if field not in data:
            raise IrError(f"Circuit IR missing required field: {field}")
    if data.get("version") != 1:
        raise IrError("Circuit IR version must be 1")
    ports_raw = _list(data["ports"], "ports")
    nets_raw = _list(data["nets"], "nets")
    instances_raw = _list(data["instances"], "instances")
    parameters_raw = _list(data["parameters"], "parameters")
    groups_raw = _list(data["matching_groups"], "matching_groups")
    for records, label in ((ports_raw, "ports"), (nets_raw, "nets"), (instances_raw, "instances"), (parameters_raw, "parameters"), (groups_raw, "matching_groups")):
        _unique(records, label)
    ports = tuple(Port(_identifier(item["id"], "port id"), str(item.get("direction", "")), str(item.get("kind", ""))) for item in ports_raw)
    nets = tuple(Net(_identifier(item["id"], "net id"), bool(item.get("critical", False))) for item in nets_raw)
    net_ids = {item.id for item in nets}
    for port in ports:
        if port.id not in net_ids:
            raise IrError(f"port {port.id} has no same-named net")
    instances = tuple(_instance(item, index, net_ids) for index, item in enumerate(instances_raw))
    parameters = tuple(_parameter(item, index) for index, item in enumerate(parameters_raw))
    groups = tuple(MatchingGroup(_identifier(item["id"], "matching group id"), tuple(_list(item.get("instances"), "matching group instances")), tuple(_list(item.get("parameters"), "matching group parameters"))) for item in groups_raw)
    return CircuitIr(
        version=1,
        metadata=_frozen_mapping(_mapping(data["metadata"], "metadata")),
        technology=_frozen_mapping(_mapping(data["technology"], "technology")),
        circuit=_frozen_mapping(_mapping(data["circuit"], "circuit")),
        ports=ports,
        nets=nets,
        instances=instances,
        parameters=parameters,
        matching_groups=groups,
        supplies=tuple(_frozen_mapping(_mapping(item, "supply")) for item in _list(data["supplies"], "supplies")),
        biases=tuple(_frozen_mapping(_mapping(item, "bias")) for item in _list(data["biases"], "biases")),
        analyses=tuple(_frozen_mapping(_mapping(item, "analysis")) for item in _list(data["analyses"], "analyses")),
        measurements=tuple(_frozen_mapping(_mapping(item, "measurement")) for item in _list(data["measurements"], "measurements")),
        constraints=tuple(_frozen_mapping(_mapping(item, "constraint")) for item in _list(data["constraints"], "constraints")),
        optimization=_frozen_mapping(_mapping(data["optimization"], "optimization")),
        provenance=_frozen_mapping(_mapping(data["provenance"], "provenance")),
        source_data=_frozen_mapping(data),
    )


def load_circuit_ir(path: str | Path) -> CircuitIr:
    try:
        return circuit_ir_from_data(load_strict_json(path))
    except StrictJsonError as exc:
        raise IrError(str(exc)) from exc

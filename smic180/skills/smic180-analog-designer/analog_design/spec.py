"""Version-1 analog design specification schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonio import StrictJsonError, load_strict_json
from .units import UnitError, parse_quantity


class SpecError(ValueError):
    """Raised when a design specification violates the version-1 contract."""


_METRIC_DIMENSIONS = {
    "gain": "gain_db",
    "ugbw": "frequency",
    "bandwidth": "frequency",
    "phase_margin": "angle",
    "power": "power",
    "slew_rate": "slew_rate",
    "output_swing": "voltage",
    "noise": "voltage",
}


@dataclass(frozen=True)
class MetricSpec:
    id: str
    kind: str
    analysis: str
    value: float
    operator: str | None = None
    status: str = "requested"


@dataclass(frozen=True)
class DesignSpec:
    version: int
    metadata: dict[str, Any]
    technology: dict[str, Any]
    circuit: dict[str, Any]
    interfaces: dict[str, Any]
    vdd: float
    temperature: float
    output_capacitance: float
    metrics: tuple[MetricSpec, ...]
    pvt: dict[str, Any]
    preferences: dict[str, Any]
    publication: dict[str, Any]


def _object(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise SpecError(f"{name} must be an object")
    return value


def _metric(item: object, index: int) -> MetricSpec:
    if not isinstance(item, dict):
        raise SpecError(f"metrics[{index}] must be an object")
    metric_id = item.get("id")
    kind = item.get("kind")
    analysis = item.get("analysis")
    if not isinstance(metric_id, str) or not metric_id:
        raise SpecError(f"metrics[{index}].id must be a non-empty string")
    if kind not in {"hard", "objective", "report"}:
        raise SpecError(f"metrics[{index}].kind is invalid")
    if not isinstance(analysis, str) or not analysis:
        raise SpecError(f"metrics[{index}].analysis must be a non-empty string")
    status = item.get("status", "requested")
    if metric_id == "phase_margin" and analysis != "stb" and status != "unverified":
        raise SpecError("phase margin requires a validated STB analysis")
    dimension = _METRIC_DIMENSIONS.get(metric_id)
    if dimension is None:
        if kind != "report":
            raise SpecError(f"metrics[{index}].id has no known dimension")
        dimension = "voltage"
    try:
        value = parse_quantity(item.get("value"), dimension)
    except UnitError as exc:
        raise SpecError(f"metrics[{index}].value is invalid: {exc}") from exc
    operator = item.get("operator")
    if operator is not None and operator not in {">=", "<=", ">", "<", "=="}:
        raise SpecError(f"metrics[{index}].operator is invalid")
    return MetricSpec(metric_id, kind, analysis, value, operator, status)


def load_design_spec(path: str | Path) -> DesignSpec:
    try:
        data = load_strict_json(path)
    except StrictJsonError as exc:
        raise SpecError(str(exc)) from exc
    if not isinstance(data, dict):
        raise SpecError("design specification must be an object")
    if data.get("version") != 1:
        raise SpecError("design specification version must be 1")
    operating = _object(data, "operating_conditions")
    loads = _object(data, "loads")
    raw_metrics = data.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics:
        raise SpecError("metrics must be a non-empty array")
    try:
        vdd = parse_quantity(operating.get("vdd"), "voltage")
        temperature = parse_quantity(operating.get("temperature"), "temperature")
        output_capacitance = parse_quantity(loads.get("output_capacitance"), "capacitance")
    except UnitError as exc:
        raise SpecError(str(exc)) from exc
    return DesignSpec(
        version=1,
        metadata=_object(data, "metadata"),
        technology=_object(data, "technology"),
        circuit=_object(data, "circuit"),
        interfaces=_object(data, "interfaces"),
        vdd=vdd,
        temperature=temperature,
        output_capacitance=output_capacitance,
        metrics=tuple(_metric(item, index) for index, item in enumerate(raw_metrics)),
        pvt=_object(data, "pvt"),
        preferences=_object(data, "preferences"),
        publication=_object(data, "publication"),
    )


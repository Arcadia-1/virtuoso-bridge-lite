"""PVT point construction and validation summaries."""
from dataclasses import dataclass
import math
import re
from typing import Any, Mapping, Sequence, Tuple

_CORNERS = {"tt", "ff", "ss", "fnsp", "snfp"}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(name + " must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(name + " must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(name + " must be finite")
    return result


def _unique(values: Sequence[Any], name: str) -> None:
    if not values:
        raise ValueError(name + " must be non-empty")
    if len(set(values)) != len(values):
        raise ValueError(name + " contains duplicates")


def _token(value: float) -> str:
    text = format(value, ".12g").lower().replace("-", "m").replace("+", "")
    return re.sub(r"[^a-z0-9]", "p", text)


@dataclass(frozen=True)
class PvtConfig:
    corners: Tuple[str, ...]
    voltages: Tuple[float, ...]
    temperatures: Tuple[float, ...]

    def __post_init__(self) -> None:
        corners = tuple(str(v).lower() for v in self.corners)
        voltages = tuple(_finite(v, "voltage") for v in self.voltages)
        temperatures = tuple(_finite(v, "temperature") for v in self.temperatures)
        _unique(corners, "corners"); _unique(voltages, "voltages"); _unique(temperatures, "temperatures")
        if any(v not in _CORNERS for v in corners): raise ValueError("unsupported corner")
        if any(v <= 0 for v in voltages): raise ValueError("voltages must be positive")
        object.__setattr__(self, "corners", corners); object.__setattr__(self, "voltages", voltages)
        object.__setattr__(self, "temperatures", temperatures)


@dataclass(frozen=True)
class PvtPoint:
    point_id: str
    corner: str
    voltage: float
    temperature: float


@dataclass(frozen=True)
class PvtWorst:
    point_id: str
    objective: float
    violation: float
    failure: Any = None


@dataclass(frozen=True)
class PvtSummary:
    overall_passed: bool
    points: Tuple[Mapping[str, Any], ...]
    worst: PvtWorst
    worst_by_spec: Mapping[str, PvtWorst]
    failures: Tuple[Mapping[str, str], ...]


def build_pvt_points(config: PvtConfig) -> Tuple[PvtPoint, ...]:
    return tuple(PvtPoint("%s-v%s-t%s" % (c, _token(v), _token(t)), c, v, t)
                 for c in config.corners for v in config.voltages for t in config.temperatures)


def summarize_pvt(points: Sequence[PvtPoint], results: Sequence[Mapping[str, Any]]) -> PvtSummary:
    ids = [p.point_id for p in points]
    if not ids or len(set(ids)) != len(ids): raise ValueError("points must be unique and non-empty")
    if len(results) != len(points): raise ValueError("PVT results are incomplete")
    by_id = {}
    for raw in results:
        if not isinstance(raw, Mapping): raise ValueError("PVT result must be a mapping")
        pid = raw.get("point_id")
        if pid not in ids or pid in by_id: raise ValueError("PVT result IDs must be unique and complete")
        if type(raw.get("success")) is not bool: raise ValueError("success must be bool")
        objective = _finite(raw.get("objective", 0.0), "objective")
        specs = raw.get("specs", {})
        if not isinstance(specs, Mapping): raise ValueError("specs must be a mapping")
        normalized = dict(raw); normalized["objective"] = objective
        for name, item in specs.items():
            if not isinstance(item, Mapping) or type(item.get("passed")) is not bool: raise ValueError("spec passed must be bool")
            _finite(item.get("violation", 0.0), "violation")
        if not raw["success"]:
            failure = raw.get("failure")
            if not isinstance(failure, Mapping) or not isinstance(failure.get("category"), str) or not isinstance(failure.get("message"), str):
                raise ValueError("failed result requires category and message")
        by_id[pid] = normalized
    ordered = tuple(by_id[pid] for pid in ids)
    def severity(item):
        violations = [_finite(v.get("violation", 0.0), "violation") for v in item["specs"].values()]
        return max([item["objective"]] + violations)
    worst_raw = max(ordered, key=severity)
    def make_worst(item, violation=None):
        return PvtWorst(item["point_id"], item["objective"], severity(item) if violation is None else violation, item.get("failure"))
    names = sorted({name for item in ordered for name in item["specs"]})
    worst_specs = {}
    for name in names:
        candidates = [(item, _finite(item["specs"][name].get("violation", 0.0), "violation")) for item in ordered if name in item["specs"]]
        chosen, value = max(candidates, key=lambda pair: pair[1]); worst_specs[name] = make_worst(chosen, value)
    failures = tuple(dict(item["failure"], point_id=item["point_id"]) for item in ordered if not item["success"])
    passed = all(item["success"] and all(spec["passed"] for spec in item["specs"].values()) for item in ordered)
    return PvtSummary(passed, ordered, make_worst(worst_raw), worst_specs, failures)

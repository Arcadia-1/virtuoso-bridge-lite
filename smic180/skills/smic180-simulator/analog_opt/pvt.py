"""PVT point construction, evaluation adaptation, and strict summaries."""
from dataclasses import asdict, dataclass
import math
import struct
from typing import Any, Mapping, Optional, Sequence, Tuple

from analog_opt.profiles import VerificationProfileConfig

_CORNERS = {"tt", "ff", "ss", "fnsp", "snfp"}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool): raise ValueError(name + " must be numeric")
    try: result = float(value)
    except (TypeError, ValueError) as exc: raise ValueError(name + " must be numeric") from exc
    if not math.isfinite(result): raise ValueError(name + " must be finite")
    return result


def _unique(values: Sequence[Any], name: str) -> None:
    if not values: raise ValueError(name + " must be non-empty")
    if len(set(values)) != len(values): raise ValueError(name + " contains duplicates")


def _float_token(value: float) -> str:
    return struct.pack(">d", value).hex()


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
class ProfilePvtJob:
    profile_id: str
    point: PvtPoint


@dataclass(frozen=True)
class PvtWorst:
    point_id: str
    objective: float
    violation: float
    failure: Optional[Mapping[str, str]] = None


@dataclass(frozen=True)
class PvtSummary:
    overall_passed: bool
    points: Tuple[Mapping[str, Any], ...]
    worst: PvtWorst
    worst_by_spec: Mapping[str, PvtWorst]
    failures: Tuple[Mapping[str, str], ...]


def build_pvt_points(config: PvtConfig) -> Tuple[PvtPoint, ...]:
    points = tuple(PvtPoint("%s-v%s-t%s" % (c, _float_token(v), _float_token(t)), c, v, t)
                   for c in config.corners for v in config.voltages for t in config.temperatures)
    _unique(tuple(point.point_id for point in points), "PVT point IDs")
    return points


def build_profile_pvt_jobs(
    profiles: Sequence[VerificationProfileConfig],
    points: Sequence[PvtPoint],
    selections: Optional[Mapping[str, Sequence[str]]] = None,
) -> Tuple[ProfilePvtJob, ...]:
    if not profiles or not points:
        raise ValueError('profiles and PVT points must be non-empty')
    if any(not isinstance(profile, VerificationProfileConfig) for profile in profiles):
        raise ValueError('profiles must contain VerificationProfileConfig values')
    profile_ids = tuple(profile.id for profile in profiles)
    _unique(profile_ids, 'profile IDs')
    point_by_id = {point.point_id: point for point in points}
    if len(point_by_id) != len(points):
        raise ValueError('PVT point IDs must be unique')
    if selections is None:
        selections = {}
    if not isinstance(selections, Mapping):
        raise ValueError('profile PVT selections must be a mapping')
    unknown_profiles = set(selections) - set(profile_ids)
    if unknown_profiles:
        raise ValueError('profile PVT selection references unknown profile')
    jobs = []
    for profile in profiles:
        if profile.pvt_policy == 'nominal_only':
            continue
        if profile.pvt_policy == 'full':
            selected_ids = tuple(point.point_id for point in points)
        elif profile.pvt_policy == 'selected':
            raw_selection = selections.get(profile.id)
            if not isinstance(raw_selection, (list, tuple)) or not raw_selection:
                raise ValueError('selected profile requires a non-empty PVT selection')
            selected_ids = tuple(raw_selection)
            _unique(selected_ids, 'profile PVT selection')
        else:
            raise ValueError('unsupported profile PVT policy')
        for point_id in selected_ids:
            if point_id not in point_by_id:
                raise ValueError('profile selection references unknown PVT point: ' + str(point_id))
            jobs.append(ProfilePvtJob(profile.id, point_by_id[point_id]))
    return tuple(jobs)


def pvt_result_from_evaluation(point: PvtPoint, result: Any, parameters: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
    required = ("objective", "success", "metrics", "metadata", "specs", "failure")
    if not all(hasattr(result, name) for name in required):
        raise ValueError("result is not EvaluationResult-compatible")
    objective = _finite(result.objective, "objective")
    if type(result.success) is not bool:
        raise ValueError("success must be bool")
    if parameters is None:
        parameters = {}
    if not all(isinstance(value, Mapping) for value in (parameters, result.metrics, result.metadata, result.specs)):
        raise ValueError("parameters, metrics, metadata, and specs must be mappings")
    failure = result.failure
    if result.success:
        if failure is not None:
            raise ValueError("successful evaluation cannot contain failure")
    else:
        if not isinstance(failure, Mapping):
            raise ValueError("failed evaluation requires failure mapping")
        category = failure.get("category")
        message = failure.get("message")
        if not isinstance(category, str) or not category.strip() or not isinstance(message, str) or not message.strip():
            raise ValueError("failure category and message must be non-empty strings")
    return {"point_id": point.point_id, "corner": point.corner, "voltage": point.voltage,
            "temperature": point.temperature, "parameters": dict(parameters), "metrics": dict(result.metrics),
            "success": result.success, "objective": objective, "specs": dict(result.specs),
            "metadata": dict(result.metadata),
            "failure": None if failure is None else dict(failure)}


def _spec_ids(expected: Optional[Sequence[str]], results: Sequence[Mapping[str, Any]]) -> Tuple[str, ...]:
    if expected is not None:
        ids = tuple(expected); _unique(ids, "expected_spec_ids")
    else:
        ids = ()
        for item in results:
            if isinstance(item, Mapping) and item.get("success") is True and isinstance(item.get("specs"), Mapping):
                ids = tuple(item["specs"].keys()); break
        _unique(ids, "inferred specification IDs")
    if any(not isinstance(v, str) or not v for v in ids): raise ValueError("specification IDs must be non-empty strings")
    return ids


def summarize_pvt(points: Sequence[PvtPoint], results: Sequence[Mapping[str, Any]],
                  expected_spec_ids: Optional[Sequence[str]] = None) -> PvtSummary:
    point_by_id = {p.point_id: p for p in points}
    if not points or len(point_by_id) != len(points): raise ValueError("points must be unique and non-empty")
    if len(results) != len(points): raise ValueError("PVT results are incomplete")
    spec_ids = _spec_ids(expected_spec_ids, results)
    by_id = {}
    for raw in results:
        if not isinstance(raw, Mapping): raise ValueError("PVT result must be a mapping")
        pid = raw.get("point_id")
        if pid not in point_by_id or pid in by_id: raise ValueError("PVT result IDs must be unique and complete")
        point = point_by_id[pid]
        if raw.get("corner") != point.corner or _finite(raw.get("voltage"), "voltage") != point.voltage or _finite(raw.get("temperature"), "temperature") != point.temperature:
            raise ValueError("PVT result condition does not match point")
        if type(raw.get("success")) is not bool: raise ValueError("success must be bool")
        if not isinstance(raw.get("parameters"), Mapping) or not isinstance(raw.get("metrics"), Mapping) or not isinstance(raw.get("metadata", {}), Mapping):
            raise ValueError("parameters, metrics, and metadata must be mappings")
        objective = _finite(raw.get("objective"), "objective")
        specs = raw.get("specs")
        if not isinstance(specs, Mapping) or set(specs) != set(spec_ids): raise ValueError("each PVT point must contain the complete specification set")
        normalized_specs = {}
        for name in spec_ids:
            item = specs[name]
            if not isinstance(item, Mapping) or type(item.get("passed")) is not bool: raise ValueError("spec passed must be bool")
            violation = _finite(item.get("violation"), "violation")
            if violation < 0 or item["passed"] != (violation == 0.0): raise ValueError("spec pass must exactly match zero nonnegative violation")
            normalized_specs[name] = dict(item, violation=violation)
        failure = raw.get("failure")
        if raw["success"]:
            if failure is not None: raise ValueError("successful PVT result cannot contain failure")
        elif not isinstance(failure, Mapping) or not isinstance(failure.get("category"), str) or not isinstance(failure.get("message"), str):
            raise ValueError("failed result requires category and message")
        by_id[pid] = {"point_id": pid, "corner": point.corner, "voltage": point.voltage,
                      "temperature": point.temperature, "parameters": dict(raw["parameters"]),
                      "metrics": dict(raw["metrics"]), "success": raw["success"], "objective": objective,
                      "specs": normalized_specs, "metadata": dict(raw.get("metadata", {})),
                      "failure": None if failure is None else dict(failure)}
    ordered = tuple(by_id[p.point_id] for p in points)
    def severity(item): return max([item["objective"]] + [v["violation"] for v in item["specs"].values()])
    worst_raw = max(ordered, key=lambda item: (not item["success"], severity(item)))
    def worst(item, value=None): return PvtWorst(item["point_id"], item["objective"], severity(item) if value is None else value, item["failure"])
    worst_specs = {}
    for name in spec_ids:
        chosen = max(ordered, key=lambda item: item["specs"][name]["violation"])
        worst_specs[name] = worst(chosen, chosen["specs"][name]["violation"])
    failures = tuple(dict(item["failure"], point_id=item["point_id"]) for item in ordered if not item["success"])
    overall = all(item["success"] and all(spec["passed"] for spec in item["specs"].values()) for item in ordered)
    return PvtSummary(overall, ordered, worst(worst_raw), worst_specs, failures)

"""Evidence-backed technology and device adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..jsonio import StrictJsonError, load_strict_json
from ..units import UnitError, parse_quantity


class TechnologyError(ValueError):
    """Raised for unresolved or unverified technology mappings."""


_REQUIRED_ADAPTER_EVIDENCE = {"master", "terminals", "cdf"}
_REQUIRED_PROFILE_EVIDENCE = {"pdk_root", "cds_lib"}


@dataclass(frozen=True)
class DeviceAdapter:
    master_ref: str
    device_class: str
    library: str | None
    cell: str | None
    view: str | None
    terminals: tuple[str, ...]
    parameter_map: Mapping[str, str]
    parameter_dimensions: Mapping[str, str]
    evidence: Mapping[str, str]
    netlist_model: str | None = None
    netlist_terminals: tuple[str, ...] = ()
    netlist_parameter_map: Mapping[str, str] = field(default_factory=dict)
    parameter_relations: Mapping[str, str] = field(default_factory=dict)
    limits: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "terminals", tuple(self.terminals))
        object.__setattr__(self, "parameter_map", MappingProxyType(dict(self.parameter_map)))
        object.__setattr__(self, "parameter_dimensions", MappingProxyType(dict(self.parameter_dimensions)))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(self, "netlist_terminals", tuple(self.netlist_terminals))
        object.__setattr__(self, "netlist_parameter_map", MappingProxyType(dict(self.netlist_parameter_map)))
        object.__setattr__(self, "parameter_relations", MappingProxyType(dict(self.parameter_relations)))
        normalized_limits = {str(name): float(value) for name, value in self.limits.items()}
        if any(not math.isfinite(value) or value < 0 for value in normalized_limits.values()):
            raise TechnologyError("device limits must be finite and non-negative")
        object.__setattr__(self, "limits", MappingProxyType(normalized_limits))
        if not self.master_ref or not self.device_class or not self.terminals:
            raise TechnologyError("device adapter identity and terminals are required")
        if set(self.parameter_map) != set(self.parameter_dimensions):
            raise TechnologyError("parameter map and dimensions must have identical keys")

    def cdf_parameter(self, generic_name: str) -> str:
        try:
            return self.parameter_map[generic_name]
        except KeyError as exc:
            raise TechnologyError(f"unknown parameter {generic_name!r} for {self.master_ref}") from exc

    def normalize(self, generic_name: str, value: object) -> float | int:
        self.cdf_parameter(generic_name)
        dimension = self.parameter_dimensions[generic_name]
        if dimension == "integer":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
                raise TechnologyError(f"parameter {generic_name!r} must be an integer")
            return int(value)
        try:
            return parse_quantity(value, dimension)
        except UnitError as exc:
            raise TechnologyError(f"invalid parameter {generic_name!r}: {exc}") from exc

    def live_evidence_complete(self) -> bool:
        return bool(self.library and self.cell and self.view and _REQUIRED_ADAPTER_EVIDENCE.issubset(self.evidence))


@dataclass(frozen=True)
class TechnologyProfile:
    name: str
    state: str
    adapters: Mapping[str, DeviceAdapter]
    evidence: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapters", MappingProxyType(dict(self.adapters)))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        if self.state not in {"unconfirmed", "confirmed"}:
            raise TechnologyError("technology profile state must be unconfirmed or confirmed")
        for key, adapter in self.adapters.items():
            if key != adapter.master_ref:
                raise TechnologyError("adapter mapping key must equal master_ref")
        if self.state == "confirmed":
            incomplete = [key for key, adapter in self.adapters.items() if not adapter.live_evidence_complete()]
            if incomplete or not _REQUIRED_PROFILE_EVIDENCE.issubset(self.evidence):
                raise TechnologyError("confirmed technology profile requires complete live evidence")

    def resolve(self, master_ref: str) -> DeviceAdapter:
        try:
            return self.adapters[master_ref]
        except KeyError as exc:
            raise TechnologyError(f"unknown master_ref: {master_ref}") from exc

    def require_live_ready(self) -> None:
        if self.state != "confirmed":
            raise TechnologyError("live operation requires a confirmed technology profile")


def technology_profile_to_dict(profile: TechnologyProfile) -> dict[str, Any]:
    return {
        "version": 1,
        "name": profile.name,
        "state": profile.state,
        "evidence": dict(profile.evidence),
        "adapters": {
            master_ref: {
                "master_ref": adapter.master_ref,
                "device_class": adapter.device_class,
                "library": adapter.library,
                "cell": adapter.cell,
                "view": adapter.view,
                "terminals": list(adapter.terminals),
                "parameter_map": dict(adapter.parameter_map),
                "parameter_dimensions": dict(adapter.parameter_dimensions),
                "evidence": dict(adapter.evidence),
                "netlist_model": adapter.netlist_model,
                "netlist_terminals": list(adapter.netlist_terminals),
                "netlist_parameter_map": dict(adapter.netlist_parameter_map),
                "parameter_relations": dict(adapter.parameter_relations),
                "limits": dict(adapter.limits),
            }
            for master_ref, adapter in sorted(profile.adapters.items())
        },
    }


def technology_profile_from_dict(data: object) -> TechnologyProfile:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise TechnologyError("technology profile version must be 1")
    adapters_data = data.get("adapters")
    if not isinstance(adapters_data, dict):
        raise TechnologyError("technology profile adapters must be an object")
    adapters: dict[str, DeviceAdapter] = {}
    try:
        for key, raw in adapters_data.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                raise TechnologyError("invalid technology adapter record")
            adapter = DeviceAdapter(
                master_ref=raw["master_ref"],
                device_class=raw["device_class"],
                library=raw.get("library"),
                cell=raw.get("cell"),
                view=raw.get("view"),
                terminals=tuple(raw["terminals"]),
                parameter_map=dict(raw["parameter_map"]),
                parameter_dimensions=dict(raw["parameter_dimensions"]),
                evidence=dict(raw["evidence"]),
                netlist_model=raw.get("netlist_model"),
                netlist_terminals=tuple(raw.get("netlist_terminals", ())),
                netlist_parameter_map=dict(raw.get("netlist_parameter_map", {})),
                parameter_relations=dict(raw.get("parameter_relations", {})),
                limits=dict(raw.get("limits", {})),
            )
            if adapter.master_ref != key:
                raise TechnologyError("adapter mapping key must equal master_ref")
            adapters[key] = adapter
        return TechnologyProfile(str(data["name"]), str(data["state"]), adapters, dict(data["evidence"]))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, TechnologyError):
            raise
        raise TechnologyError(f"invalid technology profile: {exc}") from exc


def write_technology_profile(path: str | Path, profile: TechnologyProfile) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(technology_profile_to_dict(profile), allow_nan=False, indent=2, sort_keys=True) + "\n"
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def load_technology_profile(path: str | Path) -> TechnologyProfile:
    try:
        return technology_profile_from_dict(load_strict_json(path))
    except StrictJsonError as exc:
        raise TechnologyError(str(exc)) from exc

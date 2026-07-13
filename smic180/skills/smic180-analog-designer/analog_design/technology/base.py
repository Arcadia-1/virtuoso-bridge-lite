"""Evidence-backed technology and device adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "terminals", tuple(self.terminals))
        object.__setattr__(self, "parameter_map", MappingProxyType(dict(self.parameter_map)))
        object.__setattr__(self, "parameter_dimensions", MappingProxyType(dict(self.parameter_dimensions)))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
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


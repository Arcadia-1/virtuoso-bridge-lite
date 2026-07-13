"""Evidence-driven live technology discovery orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .base import DeviceAdapter, TechnologyError, TechnologyProfile


class DiscoveryError(ValueError):
    """Raised when live evidence cannot prove a unique complete profile."""


@dataclass(frozen=True)
class DiscoveryRequest:
    pdk_roots: tuple[str, ...]
    cds_lib_candidates: tuple[str, ...]
    device_candidates: Mapping[str, tuple[tuple[str, str, str], ...]]


class DiscoveryClient(Protocol):
    def existing_paths(self, paths: tuple[str, ...]) -> list[str]: ...
    def probe_device(self, master_ref: str, candidates: tuple[tuple[str, str, str], ...]) -> dict[str, Any] | None: ...


def discover_technology(client: DiscoveryClient, request: DiscoveryRequest, *, plan_only: bool = False) -> TechnologyProfile | dict[str, Any]:
    if plan_only:
        return {
            "pdk_roots": list(request.pdk_roots),
            "cds_lib_candidates": list(request.cds_lib_candidates),
            "device_candidates": {key: [list(item) for item in value] for key, value in request.device_candidates.items()},
        }
    roots = client.existing_paths(request.pdk_roots)
    if not roots:
        raise DiscoveryError("no requested PDK root exists")
    if len(roots) != 1:
        raise DiscoveryError("multiple PDK roots exist; resolve the conflict explicitly")
    cds_libs = client.existing_paths(request.cds_lib_candidates)
    matching_libs = [path for path in cds_libs if path.startswith(roots[0].rstrip("/") + "/")]
    if len(matching_libs) != 1:
        raise DiscoveryError("exactly one cds.lib under the selected PDK root is required")
    adapters: dict[str, DeviceAdapter] = {}
    for master_ref, candidates in request.device_candidates.items():
        probe = client.probe_device(master_ref, candidates)
        if not isinstance(probe, dict):
            raise DiscoveryError(f"device discovery is incomplete for {master_ref}")
        required = {"device_class", "library", "cell", "view", "terminals", "parameter_map", "parameter_dimensions", "evidence"}
        missing = sorted(required - set(probe))
        if missing:
            raise DiscoveryError(f"device discovery is incomplete for {master_ref}: {', '.join(missing)}")
        try:
            adapter = DeviceAdapter(
                master_ref=master_ref,
                device_class=str(probe["device_class"]),
                library=str(probe["library"]),
                cell=str(probe["cell"]),
                view=str(probe["view"]),
                terminals=tuple(probe["terminals"]),
                parameter_map=dict(probe["parameter_map"]),
                parameter_dimensions=dict(probe["parameter_dimensions"]),
                evidence=dict(probe["evidence"]),
            )
        except (TechnologyError, TypeError, ValueError) as exc:
            raise DiscoveryError(f"invalid live evidence for {master_ref}: {exc}") from exc
        adapters[master_ref] = adapter
    try:
        return TechnologyProfile("smic180", "confirmed", adapters, {"pdk_root": roots[0], "cds_lib": matching_libs[0]})
    except TechnologyError as exc:
        raise DiscoveryError(str(exc)) from exc

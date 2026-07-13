"""Guarded execution of an already verified schematic plan."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Protocol

from ..artifacts import ArtifactStore
from .plan import SchematicPlan


class MaterializationError(ValueError):
    """Raised when a Virtuoso creation or verification gate fails."""


class MaterializationClient(Protocol):
    def cell_exists(self, library: str, cell: str, view: str) -> bool: ...
    def preflight_master(self, library: str, cell: str, view: str, terminals: tuple[str, ...]) -> bool: ...
    def create_schematic(self, plan: SchematicPlan) -> None: ...
    def apply_cdf(self, plan: SchematicPlan) -> None: ...
    def save_close(self, library: str, cell: str) -> None: ...
    def reopen_readback(self, plan: SchematicPlan) -> dict[str, dict[str, Any]]: ...
    def schcheck_save(self, library: str, cell: str) -> bool: ...
    def export_si(self, library: str, cell: str, output: Path) -> Path: ...


def _plain_plan(plan: SchematicPlan) -> dict[str, Any]:
    return {
        "library": plan.library,
        "target_cell": plan.target_cell,
        "source_cell": plan.source_cell,
        "view": plan.view,
        "ports": dict(plan.ports),
        "nets": list(plan.nets),
        "instances": [{"id": item.id, "library": item.library, "cell": item.cell, "view": item.view, "terminals": dict(item.terminals), "cdf_values": dict(item.cdf_values), "cdf_dimensions": dict(item.cdf_dimensions)} for item in plan.instances],
        "expected_readback": {key: dict(value) for key, value in plan.expected_readback.items()},
    }


def _equal_value(expected: Any, actual: Any) -> bool:
    resolution = 0.0
    if isinstance(actual, dict) and {"value", "raw", "resolution"}.issubset(actual):
        resolution = float(actual["resolution"])
        actual = actual["value"]
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) and isinstance(actual, (int, float)) and not isinstance(actual, bool):
        return math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=max(1e-15, resolution * 0.5))
    return expected == actual


def _validate_readback(expected: dict[str, dict[str, Any]], actual: dict[str, dict[str, Any]]) -> None:
    if set(expected) != set(actual):
        raise MaterializationError("CDF readback instance set differs from plan")
    for instance, values in expected.items():
        read = actual.get(instance, {})
        if set(values) != set(read):
            raise MaterializationError(f"CDF readback parameters differ for {instance}")
        for name, value in values.items():
            if not _equal_value(value, read[name]):
                raise MaterializationError(f"CDF readback mismatch for {instance}.{name}")


def materialize_schematic(client: MaterializationClient, plan: SchematicPlan, evidence_dir: str | Path, *, plan_only: bool = False, replace: bool = False) -> dict[str, Any]:
    output = Path(evidence_dir)
    store = ArtifactStore(output)
    plan_data = _plain_plan(plan)
    if plan_only:
        return {"status": "planned", "plan": plan_data}
    output.mkdir(parents=True, exist_ok=True)
    for stale_name in (
        "cdf_readback.json",
        "schcheck.json",
        "exported_netlist.scs",
        "cdf_roundtrip.confirmed.json",
        "schematic_checked.confirmed.json",
    ):
        (output / stale_name).unlink(missing_ok=True)
    plan_path = store.write_json(output / "schematic_plan.json", plan_data)
    exists = client.cell_exists(plan.library, plan.target_cell, plan.view)
    if exists and not replace:
        raise MaterializationError(f"target cell already exists: {plan.library}/{plan.target_cell}/{plan.view}")
    for instance in plan.instances:
        if not client.preflight_master(instance.library, instance.cell, instance.view, tuple(instance.terminals)):
            raise MaterializationError(f"master preflight failed for {instance.id}")
    client.create_schematic(plan)
    client.apply_cdf(plan)
    client.save_close(plan.library, plan.target_cell)
    readback = client.reopen_readback(plan)
    _validate_readback({key: dict(value) for key, value in plan.expected_readback.items()}, readback)
    readback_path = store.write_json(output / "cdf_readback.json", readback)
    if not client.schcheck_save(plan.library, plan.target_cell):
        raise MaterializationError("schCheck failed")
    check_path = store.write_json(output / "schcheck.json", {"passed": True})
    netlist = client.export_si(plan.library, plan.target_cell, output / "exported_netlist.scs")
    if not Path(netlist).is_file():
        raise MaterializationError("si export did not produce a netlist")
    store.confirm(output / "cdf_roundtrip.confirmed.json", "cdf_roundtrip_passed", [plan_path, readback_path])
    store.confirm(output / "schematic_checked.confirmed.json", "schematic_checked", [check_path, netlist])
    return {"status": "materialized", "cdf_roundtrip_passed": True, "schematic_checked": True, "exported_netlist": str(Path(netlist))}

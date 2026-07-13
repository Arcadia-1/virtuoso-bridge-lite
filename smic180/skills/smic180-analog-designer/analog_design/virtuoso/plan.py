"""Pure-data Virtuoso schematic plan generation."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ..ir import CircuitIr
from ..technology.base import TechnologyError, TechnologyProfile


class PlanError(ValueError):
    """Raised when frozen IR cannot be safely planned for Virtuoso."""


@dataclass(frozen=True)
class SchematicInstancePlan:
    id: str
    library: str
    cell: str
    view: str
    terminals: Mapping[str, str]
    cdf_values: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "terminals", MappingProxyType(dict(self.terminals)))
        object.__setattr__(self, "cdf_values", MappingProxyType(dict(self.cdf_values)))


@dataclass(frozen=True)
class SchematicPlan:
    library: str
    target_cell: str
    source_cell: str
    view: str
    instances: tuple[SchematicInstancePlan, ...]
    expected_readback: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_readback", MappingProxyType({key: MappingProxyType(dict(value)) for key, value in self.expected_readback.items()}))


def build_schematic_plan(ir: CircuitIr, profile: TechnologyProfile, library: str, target_cell: str, *, source_cell: str, view: str = "schematic") -> SchematicPlan:
    if not library or not target_cell or not source_cell:
        raise PlanError("library, source cell, and target cell are required")
    if target_cell == source_cell:
        raise PlanError("source and target cells must differ")
    try:
        profile.require_live_ready()
    except TechnologyError as exc:
        raise PlanError(str(exc)) from exc
    plans: list[SchematicInstancePlan] = []
    expected: dict[str, dict[str, Any]] = {}
    for instance in ir.instances:
        try:
            adapter = profile.resolve(instance.master_ref)
        except TechnologyError as exc:
            raise PlanError(str(exc)) from exc
        missing = set(instance.terminals) - set(adapter.terminals)
        if missing:
            raise PlanError(f"instance {instance.id} has unverified terminals: {', '.join(sorted(missing))}")
        cdf_values: dict[str, Any] = {}
        for generic_name, value in instance.logical_parameters.items():
            if generic_name not in adapter.parameter_map:
                continue
            cdf_name = adapter.cdf_parameter(generic_name)
            cdf_values[cdf_name] = adapter.normalize(generic_name, value)
        plan = SchematicInstancePlan(instance.id, str(adapter.library), str(adapter.cell), str(adapter.view), instance.terminals, cdf_values)
        plans.append(plan)
        expected[instance.id] = dict(cdf_values)
    return SchematicPlan(library, target_cell, source_cell, view, tuple(plans), expected)

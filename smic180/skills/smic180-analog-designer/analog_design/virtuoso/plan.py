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
    cdf_dimensions: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "terminals", MappingProxyType(dict(self.terminals)))
        object.__setattr__(self, "cdf_values", MappingProxyType(dict(self.cdf_values)))
        object.__setattr__(self, "cdf_dimensions", MappingProxyType(dict(self.cdf_dimensions)))
        if set(self.cdf_values) != set(self.cdf_dimensions):
            raise PlanError("CDF values and dimensions must have identical keys")


@dataclass(frozen=True)
class SchematicPlan:
    library: str
    target_cell: str
    source_cell: str
    view: str
    ports: Mapping[str, str]
    nets: tuple[str, ...]
    instances: tuple[SchematicInstancePlan, ...]
    expected_readback: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ports", MappingProxyType(dict(self.ports)))
        object.__setattr__(self, "nets", tuple(self.nets))
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
        missing = set(instance.terminals) - set(adapter.terminal_map)
        if missing:
            raise PlanError(f"instance {instance.id} has unverified terminals: {', '.join(sorted(missing))}")
        terminals = {adapter.terminal_map[name]: net for name, net in instance.terminals.items()}
        if set(terminals) != set(adapter.terminals):
            raise PlanError(f"instance {instance.id} does not cover every verified master terminal")
        cdf_values: dict[str, Any] = dict(instance.cdf_expectations)
        if not cdf_values:
            for generic_name, value in instance.logical_parameters.items():
                if generic_name not in adapter.parameter_map:
                    continue
                cdf_name = adapter.cdf_parameter(generic_name)
                cdf_values[cdf_name] = adapter.normalize(generic_name, value)
        dimensions_by_cdf = {
            adapter.parameter_map[generic_name]: adapter.parameter_dimensions[generic_name]
            for generic_name in adapter.parameter_map
        }
        cdf_dimensions = {
            name: dimensions_by_cdf.get(name, "string" if isinstance(value, str) else "dimensionless")
            for name, value in cdf_values.items()
        }
        plan = SchematicInstancePlan(
            instance.id,
            str(adapter.library),
            str(adapter.cell),
            str(adapter.view),
            terminals,
            cdf_values,
            cdf_dimensions,
        )
        plans.append(plan)
        expected[instance.id] = dict(cdf_values)
    ports = {port.id: port.direction for port in ir.ports}
    nets = tuple(net.id for net in ir.nets)
    return SchematicPlan(library, target_cell, source_cell, view, ports, nets, tuple(plans), expected)

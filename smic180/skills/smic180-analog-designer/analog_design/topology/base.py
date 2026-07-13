"""Topology plugin data contracts."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class TopologyError(ValueError):
    """Raised for unsupported or inconsistent topology requests."""


@dataclass(frozen=True)
class TopologyInstance:
    id: str
    role: str
    device_class: str
    terminals: Mapping[str, str]
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "terminals", MappingProxyType(dict(self.terminals)))


@dataclass(frozen=True)
class TopologyPlan:
    id: str
    ports: tuple[str, ...]
    nets: tuple[str, ...]
    instances: tuple[TopologyInstance, ...]
    matching_groups: Mapping[str, tuple[str, ...]]
    selection_basis: tuple[str, ...]
    known_limits: tuple[str, ...]

    def __post_init__(self) -> None:
        groups = {name: tuple(members) for name, members in self.matching_groups.items()}
        object.__setattr__(self, "matching_groups", MappingProxyType(groups))

    def instance(self, identity: str) -> TopologyInstance:
        for item in self.instances:
            if item.id == identity:
                return item
        raise KeyError(identity)

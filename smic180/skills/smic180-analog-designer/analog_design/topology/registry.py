"""Topology plugin registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .base import TopologyError, TopologyPlan
from .two_stage_miller import build_two_stage_miller


TopologyFactory = Callable[[Mapping[str, object]], TopologyPlan]


class TopologyRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, TopologyFactory] = {}

    def register(self, identity: str, factory: TopologyFactory) -> None:
        if not identity or identity in self._factories:
            raise TopologyError(f"topology registration is invalid: {identity}")
        self._factories[identity] = factory

    def create(self, identity: str, options: Mapping[str, object]) -> TopologyPlan:
        try:
            factory = self._factories[identity]
        except KeyError as exc:
            raise TopologyError(f"unknown topology: {identity}") from exc
        return factory(options)


def default_registry() -> TopologyRegistry:
    registry = TopologyRegistry()
    registry.register("two_stage_miller", build_two_stage_miller)
    return registry

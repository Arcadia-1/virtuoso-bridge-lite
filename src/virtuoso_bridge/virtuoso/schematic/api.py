"""High-level schematic API facade bound to a shared bridge client/session."""

from __future__ import annotations

from typing import Any

from virtuoso_bridge.virtuoso.ops import open_cell_view, save_current_cellview
from virtuoso_bridge.virtuoso.schematic.editor import SchematicEditor
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_check,
    schematic_create_inst_by_master_name,
    schematic_create_pin,
    schematic_create_pin_at_instance_term,
    schematic_create_wire,
    schematic_create_wire_between_instance_terms,
    schematic_create_wire_label,
    schematic_label_instance_term,
)

class SchematicOps:
    """Schematic operations facade backed by a shared bridge client or bridge session."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def edit(
        self,
        lib: str,
        cell: str,
        view: str = "schematic",
        mode: str = "w",
        timeout: int = 60,
    ) -> SchematicEditor:
        """Return a declarative schematic editor bound to the shared session."""
        return SchematicEditor(self._owner, lib, cell, view=view, mode=mode, timeout=timeout)

    def open(
        self,
        lib: str,
        cell: str,
        *,
        view: str = "schematic",
        view_type: str | None = None,
        mode: str = "w",
        timeout: int = 30,
    ) -> Any:
        """Open a schematic cellview and bind it to ``cv``."""
        return self._owner.execute_skill(
            open_cell_view(lib, cell, view=view, view_type=view_type, mode=mode),
            timeout=timeout,
        )

    def save(self, timeout: int = 30) -> Any:
        """Save the current schematic cellview."""
        return self._owner.execute_skill(save_current_cellview(), timeout=timeout)

    def check(self, timeout: int = 30) -> Any:
        """Run schematic check on the currently bound cellview."""
        return self._owner.execute_skill(schematic_check(), timeout=timeout)

    def add_instance(
        self,
        lib: str,
        cell: str,
        xy: tuple[float, float],
        *,
        orientation: str = "R0",
        view: str = "symbol",
        name: str = "",
        timeout: int = 30,
    ) -> Any:
        """Create a schematic instance by master name."""
        return self._owner.execute_skill(
            schematic_create_inst_by_master_name(
                lib,
                cell,
                view,
                name,
                xy[0],
                xy[1],
                orientation,
            ),
            timeout=timeout,
        )

    def add_wire(
        self,
        points: list[tuple[float, float]],
        *,
        timeout: int = 30,
    ) -> Any:
        """Create a schematic wire from a point list."""
        return self._owner.execute_skill(schematic_create_wire(points), timeout=timeout)

    def add_label(
        self,
        xy: tuple[float, float],
        text: str,
        *,
        justification: str = "lowerLeft",
        rotation: str = "R0",
        timeout: int = 30,
    ) -> Any:
        """Create a schematic wire label."""
        return self._owner.execute_skill(
            schematic_create_wire_label(xy[0], xy[1], text, justification, rotation),
            timeout=timeout,
        )

    def add_pin(
        self,
        name: str,
        xy: tuple[float, float],
        *,
        orientation: str = "R0",
        direction: str = "inputOutput",
        timeout: int = 30,
    ) -> Any:
        """Create a schematic pin."""
        return self._owner.execute_skill(
            schematic_create_pin(name, xy[0], xy[1], orientation, direction=direction),
            timeout=timeout,
        )

    def add_pin_to_instance_term(
        self,
        instance_name: str,
        term_name: str,
        pin_name: str,
        *,
        direction: str = "inputOutput",
        orientation: str = "R0",
        timeout: int = 30,
    ) -> Any:
        """Create a schematic pin at an instance terminal center."""
        return self._owner.execute_skill(
            schematic_create_pin_at_instance_term(
                instance_name,
                term_name,
                pin_name,
                direction=direction,
                orientation=orientation,
            ),
            timeout=timeout,
        )

    def add_wire_between_instance_terms(
        self,
        from_instance: str,
        from_term: str,
        to_instance: str,
        to_term: str,
        *,
        timeout: int = 30,
    ) -> Any:
        """Create a wire directly between two instance terminals."""
        return self._owner.execute_skill(
            schematic_create_wire_between_instance_terms(
                from_instance,
                from_term,
                to_instance,
                to_term,
            ),
            timeout=timeout,
        )

    def add_net_label_to_instance_term(
        self,
        instance_name: str,
        term_name: str,
        net_name: str,
        *,
        timeout: int = 30,
    ) -> Any:
        """Create a labeled wire stub at an instance terminal."""
        return self._owner.execute_skill(
            schematic_label_instance_term(instance_name, term_name, net_name),
            timeout=timeout,
        )

    def add_net_label_to_transistor(
        self,
        instance_name: str,
        drain_net: str | None,
        gate_net: str | None,
        source_net: str | None,
        body_net: str | None,
        *,
        timeout: int = 30,
    ) -> Any:
        """Label MOS terminals in SPICE/CDL order: D, G, S, B."""
        commands = [
            schematic_label_instance_term(instance_name, term_name, net_name)
            for term_name, net_name in (
                ("D", drain_net),
                ("G", gate_net),
                ("S", source_net),
                ("B", body_net),
            )
            if net_name
        ]
        return self._owner.execute_operations(commands, timeout=timeout)

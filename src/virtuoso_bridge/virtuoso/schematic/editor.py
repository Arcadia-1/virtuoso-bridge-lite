"""High-level schematic editor for declarative Virtuoso operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from virtuoso_bridge.virtuoso.ops import open_cell_view, save_current_cellview
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_check,
    schematic_create_inst,
    schematic_create_inst_by_master_name,
    schematic_create_pin,
    schematic_create_pin_at_instance_term,
    schematic_create_wire_between_instance_terms,
    schematic_label_instance_term,
    schematic_create_wire,
    schematic_create_wire_label,
)

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient

def _ensure_operation_response(response: Any, *, context: str) -> None:
    from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
    if isinstance(response, VirtuosoResult):
        if response.status != ExecutionStatus.SUCCESS:
            errors = response.errors or ["unknown failure"]
            raise RuntimeError(f"{context} failed: {errors[0]}")
        return
    if not response.get("ok", False):
        raise RuntimeError(f"{context} failed: {response.get('error', 'request failed')}")
    result = response.get("result", {})
    if result.get("status") != "success":
        errors = result.get("errors") or [result.get("status", "unknown failure")]
        raise RuntimeError(f"{context} failed: {errors[0]}")

class SchematicEditor:
    """Context manager for schematic editing operations."""

    def __init__(
        self,
        client: VirtuosoClient,
        lib: str,
        cell: str,
        view: str = "schematic",
        mode: str = "w",
        timeout: int = 60,
    ) -> None:
        self.client = client
        self.lib = lib
        self.cell = cell
        self.view = view
        self.mode = mode
        self.timeout = timeout
        self.commands: list[str] = []

    def __enter__(self) -> SchematicEditor:
        self.commands.append(open_cell_view(self.lib, self.cell, view=self.view, mode=self.mode))
        return self

    def add_instance(
        self,
        lib: str,
        cell: str,
        xy: tuple[float, float],
        orientation: str = "R0",
        view: str = "symbol",
        name: str = "",
    ) -> None:
        """Add a schematic instance."""
        self.commands.append(
            schematic_create_inst_by_master_name(
                lib,
                cell,
                view,
                name,
                xy[0],
                xy[1],
                orientation,
            )
        )

    def add_wire(self, points: Iterable[tuple[float, float]]) -> None:
        """Add a schematic wire."""
        self.commands.append(schematic_create_wire(points))

    def add_label(
        self,
        xy: tuple[float, float],
        text: str,
        justification: str = "lowerLeft",
        rotation: str = "R0",
    ) -> None:
        """Add a wire label."""
        self.commands.append(
            schematic_create_wire_label(xy[0], xy[1], text, justification, rotation)
        )

    def add_net_label_to_instance_term(
        self,
        instance_name: str,
        term_name: str,
        net_name: str,
    ) -> None:
        """Add a net label at an instance terminal center."""
        self.commands.append(
            schematic_label_instance_term(instance_name, term_name, net_name)
        )

    def add_net_label_to_transistor(
        self,
        instance_name: str,
        drain_net: str | None,
        gate_net: str | None,
        source_net: str | None,
        body_net: str | None,
    ) -> None:
        """Label MOS terminals in SPICE/CDL order: D, G, S, B."""
        for term_name, net_name in (
            ("D", drain_net),
            ("G", gate_net),
            ("S", source_net),
            ("B", body_net),
        ):
            if net_name:
                self.add_net_label_to_instance_term(instance_name, term_name, net_name)

    def add_pin(
        self,
        name: str,
        xy: tuple[float, float],
        orientation: str = "R0",
        direction: str = "inputOutput",
    ) -> None:
        """Add a schematic pin."""
        self.commands.append(
            schematic_create_pin(name, xy[0], xy[1], orientation, direction=direction)
        )

    def add_pin_to_instance_term(
        self,
        instance_name: str,
        term_name: str,
        pin_name: str,
        *,
        direction: str = "inputOutput",
        orientation: str = "R0",
    ) -> None:
        """Add a schematic pin at an instance terminal center."""
        self.commands.append(
            schematic_create_pin_at_instance_term(
                instance_name,
                term_name,
                pin_name,
                direction=direction,
                orientation=orientation,
            )
        )

    def add_wire_between_instance_terms(
        self,
        from_instance: str,
        from_term: str,
        to_instance: str,
        to_term: str,
    ) -> None:
        """Add a wire directly between two instance terminals."""
        self.commands.append(
            schematic_create_wire_between_instance_terms(
                from_instance,
                from_term,
                to_instance,
                to_term,
            )
        )

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.commands.append(schematic_check())
            self.commands.append(save_current_cellview())
            response = self.client.execute_operations(self.commands, timeout=self.timeout)
            _ensure_operation_response(response, context="schematic edit")

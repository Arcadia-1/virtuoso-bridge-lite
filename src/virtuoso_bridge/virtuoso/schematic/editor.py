"""Schematic editor — context manager for batch SKILL operations.

Usage:
    from virtuoso_bridge.virtuoso.schematic.ops import *

    with client.schematic.edit(LIB, CELL) as sch:
        sch.add(schematic_create_inst_by_master_name("tsmcN28", "nch_ulvt_mac", "symbol", "M0", 0, 0, "R0"))
        sch.add(schematic_label_instance_term("M0", "D", "OUT"))
        sch.add(schematic_create_pin("IN", -1.0, 0.75, "R0", direction="input"))
        # schCheck + dbSave on exit

    Convenience shortcut for MOS terminals:
        sch.add_net_label_to_transistor("M0", drain_net="OUT", gate_net="IN", source_net="VSS", body_net="VSS")
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from virtuoso_bridge.virtuoso.ops import open_cell_view, save_current_cellview
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_check,
    schematic_label_instance_term,
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
    """Context manager: open cellview → accumulate commands → check → save."""

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

    def add(self, skill_cmd: str) -> None:
        """Append a SKILL command to the batch."""
        self.commands.append(skill_cmd)

    def add_net_label_to_transistor(
        self,
        instance_name: str,
        drain_net: str | None = None,
        gate_net: str | None = None,
        source_net: str | None = None,
        body_net: str | None = None,
    ) -> None:
        """Label MOS terminals D/G/S/B with net names."""
        for term, net in (("D", drain_net), ("G", gate_net), ("S", source_net), ("B", body_net)):
            if net:
                self.commands.append(schematic_label_instance_term(instance_name, term, net))

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.commands.append(schematic_check())
            self.commands.append(save_current_cellview())
            response = self.client.execute_operations(self.commands, timeout=self.timeout)
            _ensure_operation_response(response, context="schematic edit")

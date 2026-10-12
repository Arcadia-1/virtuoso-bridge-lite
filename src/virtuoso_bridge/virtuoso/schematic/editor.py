"""Schematic editor — context manager for batch SKILL operations.

Usage:
    from virtuoso_bridge.virtuoso.schematic.ops import *

    with client.schematic.create(LIB, CELL) as sch:
        sch.add(schematic_create_inst_by_master_name("tsmcN28", "nch_ulvt_mac", "symbol", "M0", 0, 0, "R0"))
        sch.add(schematic_label_instance_term("M0", "D", "OUT"))
        sch.add(schematic_create_pin("IN", -1.0, 0.75, "R0", direction="input"))
        # schCheck + dbSave on exit

    Convenience shortcut for MOS terminals:
        sch.add_net_label_to_transistor("M0", drain_net="OUT", gate_net="IN", source_net="VSS", body_net="VSS")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from virtuoso_bridge.virtuoso.editor import (
    close_editor_cellview,
    ensure_operation_response,
    remember_editor_cellview,
)
from virtuoso_bridge.virtuoso.ops import open_cell_view, save_current_cellview
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_check,
    schematic_label_instance_term,
)

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient

# Body terminal names seen across PDKs, tried case-insensitively after "B".
_BODY_TERMINAL_ALIASES = ("BULK", "BODY", "SUB")


class SchematicEditor:
    """Context manager: open cellview → accumulate commands → check → save."""

    def __init__(
        self,
        client: VirtuosoClient,
        lib: str,
        cell: str,
        view: str = "schematic",
        mode: str = "a",
        timeout: int = 60,
        keep_open: bool = False,
    ) -> None:
        self.client = client
        self.keep_open = keep_open
        self.lib = lib
        self.cell = cell
        self.view = view
        self.mode = mode
        self.timeout = timeout
        self.commands: list[str] = []

    def __enter__(self) -> SchematicEditor:
        self.commands.append(open_cell_view(self.lib, self.cell, view=self.view, mode=self.mode))
        self.commands.append(remember_editor_cellview(opened=True))
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
        *,
        terminal_names: Mapping[str, str] | None = None,
    ) -> None:
        """Label MOS terminals D/G/S/B with net names.

        Terminal names are matched case-insensitively (``D`` or ``d``). The
        body is labelled on a ``B``, ``BULK``, ``BODY`` or ``SUB`` terminal;
        a symbol without one (a 3-terminal device whose body comes from its
        model or an inherited connection) is left as it is. A missing drain,
        gate or source terminal is an error. ``terminal_names`` names the
        terminal for a role on other PDKs, e.g. ``{"B": "sx"}``.
        """
        names = {role.upper(): name for role, name in (terminal_names or {}).items()}
        for role, net in (("D", drain_net), ("G", gate_net), ("S", source_net), ("B", body_net)):
            if not net:
                continue
            if role in names:
                command = schematic_label_instance_term(instance_name, names[role], net)
            elif role == "B":
                command = schematic_label_instance_term(
                    instance_name, role, net, aliases=_BODY_TERMINAL_ALIASES, missing_ok=True,
                )
            else:
                command = schematic_label_instance_term(instance_name, role, net)
            self.commands.append(command)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.commands.append(schematic_check())
            self.commands.append(save_current_cellview())
            if not self.keep_open:
                self.commands.append(close_editor_cellview())
            response = self.client.execute_operations(self.commands, timeout=self.timeout)
            ensure_operation_response(response, context="schematic edit")

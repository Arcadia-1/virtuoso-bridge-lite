"""SKILL builders for Cadence Virtuoso schematic editing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from virtuoso_bridge.virtuoso.schematic.editor import SchematicEditor
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_check,
    schematic_create_net_expression,
    schematic_create_inst,
    schematic_create_inst_by_master_name,
    schematic_create_pin,
    schematic_create_pin_at_instance_term,
    schematic_create_wire_between_instance_terms,
    schematic_create_net_stub,
    schematic_label_instance_term,
    schematic_label_instance_term_offset,
    schematic_create_wire,
    schematic_create_wire_label,
    schematic_set_netset_property,
)
from virtuoso_bridge.virtuoso.schematic.netlist import (
    SchematicNetlistExportResult,
    export_schematic_netlist,
    schematic_export_netlist_skill,
)

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


class SchematicOps:
    """Attached to VirtuosoClient as ``client.schematic``."""

    def __init__(self, owner: VirtuosoClient) -> None:
        self._owner = owner

    def edit(self, lib: str, cell: str, view: str = "schematic",
             mode: str = "w", timeout: int = 60) -> SchematicEditor:
        """Return a SchematicEditor context manager."""
        return SchematicEditor(self._owner, lib, cell, view=view, mode=mode, timeout=timeout)

    def export_netlist(
        self,
        lib: str,
        cell: str,
        output_dir: str | Path,
        *,
        view: str = "schematic",
        simulator: str = "spectre",
        recreate_all: bool = True,
        timeout: int = 120,
    ) -> SchematicNetlistExportResult:
        """Export a schematic netlist package through Virtuoso's netlister.

        The returned dictionary includes the Virtuoso-side ``source_file`` and
        ``source_dir``, the local ``output_dir`` and ``input_file``, plus the raw
        SKILL and download results. Existing ``output_dir`` contents are kept
        until the new package has downloaded and ``input.scs`` is present.
        """
        return export_schematic_netlist(
            self._owner,
            lib,
            cell,
            output_dir,
            view=view,
            simulator=simulator,
            recreate_all=recreate_all,
            timeout=timeout,
        )


__all__ = [
    "SchematicOps",
    "SchematicEditor",
    "schematic_create_inst",
    "schematic_create_inst_by_master_name",
    "schematic_create_wire",
    "schematic_create_wire_label",
    "schematic_create_net_expression",
    "schematic_set_netset_property",
    "schematic_label_instance_term",
    "schematic_label_instance_term_offset",
    "schematic_create_pin",
    "schematic_create_pin_at_instance_term",
    "schematic_create_wire_between_instance_terms",
    "schematic_create_net_stub",
    "schematic_check",
    "SchematicNetlistExportResult",
    "schematic_export_netlist_skill",
    "export_schematic_netlist",
]

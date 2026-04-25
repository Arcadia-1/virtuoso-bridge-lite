"""SKILL builders for Cadence Virtuoso schematic editing."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from virtuoso_bridge.virtuoso.schematic.editor import SchematicEditor
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


class SchematicOps:
    """Attached to VirtuosoClient as ``client.schematic``."""

    def __init__(self, owner: VirtuosoClient) -> None:
        self._owner = owner

    def edit(self, lib: str, cell: str, view: str = "schematic",
             mode: str = "w", timeout: int = 60) -> SchematicEditor:
        """Return a SchematicEditor context manager."""
        return SchematicEditor(self._owner, lib, cell, view=view, mode=mode, timeout=timeout)


__all__ = [
    "SchematicOps",
    "SchematicEditor",
    "schematic_create_inst",
    "schematic_create_inst_by_master_name",
    "schematic_create_wire",
    "schematic_create_wire_label",
    "schematic_label_instance_term",
    "schematic_create_pin",
    "schematic_create_pin_at_instance_term",
    "schematic_create_wire_between_instance_terms",
    "schematic_check",
]

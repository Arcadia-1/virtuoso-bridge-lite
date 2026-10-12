"""SKILL builders for Cadence Virtuoso symbol editing."""

from __future__ import annotations

import warnings
from typing import Any, TYPE_CHECKING

from virtuoso_bridge.virtuoso.symbol.editor import SymbolEditor
from virtuoso_bridge.virtuoso.symbol.generator import (
    SymbolGenerationAction,
    SymbolGenerationResult,
    SymbolPinSort,
    generate_symbol_from_schematic,
    symbol_generate_from_schematic_skill,
)
from virtuoso_bridge.virtuoso.symbol.ops import (
    symbol_check,
    symbol_create_ellipse,
    symbol_create_instance_label,
    symbol_create_label,
    symbol_create_line,
    symbol_create_logical_label,
    symbol_create_pin,
    symbol_create_pin_name,
    symbol_create_polygon,
    symbol_create_rect,
    symbol_create_selection_box,
    symbol_set_term_order,
)
from virtuoso_bridge.virtuoso.symbol.reader import (
    parse_symbol_ports_output,
    read_symbol_ports,
    symbol_read_ports_skill,
)

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


class SymbolOps:
    """Attached to VirtuosoClient as ``client.symbol``."""

    def __init__(self, owner: VirtuosoClient) -> None:
        self._owner = owner

    def create(
        self,
        lib: str,
        cell: str,
        view: str = "symbol",
        view_type: str = "schematicSymbol",
        timeout: int = 60,
        keep_open: bool = False,
    ) -> SymbolEditor:
        """Create a symbol editor, replacing an existing view if present."""
        return SymbolEditor(
            self._owner,
            lib,
            cell,
            view=view,
            view_type=view_type,
            mode="w",
            timeout=timeout,
            keep_open=keep_open,
        )

    def modify(
        self,
        lib: str,
        cell: str,
        view: str = "symbol",
        view_type: str = "schematicSymbol",
        timeout: int = 60,
        keep_open: bool = False,
    ) -> SymbolEditor:
        """Open a symbol editor in append mode without clearing its view."""
        return SymbolEditor(
            self._owner,
            lib,
            cell,
            view=view,
            view_type=view_type,
            mode="a",
            timeout=timeout,
            keep_open=keep_open,
        )

    def edit(
        self,
        lib: str,
        cell: str,
        view: str = "symbol",
        view_type: str = "schematicSymbol",
        mode: str = "a",
        timeout: int = 60,
        keep_open: bool = False,
    ) -> SymbolEditor:
        """Deprecated compatibility wrapper for :meth:`create` / :meth:`modify`."""
        warnings.warn(
            "client.symbol.edit() is deprecated; use create() or modify() explicitly.",
            DeprecationWarning,
            stacklevel=2,
        )
        return SymbolEditor(
            self._owner,
            lib,
            cell,
            view=view,
            view_type=view_type,
            mode=mode,
            timeout=timeout,
            keep_open=keep_open,
        )

    def generate_from_schematic(
        self,
        lib: str,
        cell: str,
        *,
        schematic_view: str = "schematic",
        symbol_view: str = "symbol",
        sort_pins: SymbolPinSort | None = None,
        overwrite: bool = False,
        timeout: int = 60,
    ) -> SymbolGenerationResult:
        """Generate and verify a symbol from a schematic cellview."""
        return generate_symbol_from_schematic(
            self._owner,
            lib,
            cell,
            schematic_view=schematic_view,
            symbol_view=symbol_view,
            sort_pins=sort_pins,
            overwrite=overwrite,
            timeout=timeout,
        )

    def read_ports(
        self,
        lib: str,
        cell: str,
        view: str = "symbol",
        view_type: str = "schematicSymbol",
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Read symbol terminals, labels, port order, and term order."""
        return read_symbol_ports(
            self._owner,
            lib,
            cell,
            view=view,
            view_type=view_type,
            timeout=timeout,
        )


__all__ = [
    "SymbolOps",
    "SymbolEditor",
    "SymbolGenerationAction",
    "SymbolGenerationResult",
    "SymbolPinSort",
    "generate_symbol_from_schematic",
    "symbol_generate_from_schematic_skill",
    "symbol_create_line",
    "symbol_create_rect",
    "symbol_create_polygon",
    "symbol_create_ellipse",
    "symbol_create_label",
    "symbol_create_pin_name",
    "symbol_create_instance_label",
    "symbol_create_logical_label",
    "symbol_create_selection_box",
    "symbol_create_pin",
    "symbol_set_term_order",
    "symbol_check",
    "symbol_read_ports_skill",
    "parse_symbol_ports_output",
    "read_symbol_ports",
]

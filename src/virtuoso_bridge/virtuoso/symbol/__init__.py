"""SKILL builders for Cadence Virtuoso symbol editing."""

from __future__ import annotations

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
    symbol_create_label,
    symbol_create_line,
    symbol_create_pin,
    symbol_create_polygon,
    symbol_create_rect,
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

    def edit(
        self,
        lib: str,
        cell: str,
        view: str = "symbol",
        view_type: str = "schematicSymbol",
        mode: str = "w",
        timeout: int = 60,
    ) -> SymbolEditor:
        """Return a SymbolEditor context manager."""
        return SymbolEditor(
            self._owner,
            lib,
            cell,
            view=view,
            view_type=view_type,
            mode=mode,
            timeout=timeout,
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
    "symbol_create_pin",
    "symbol_set_term_order",
    "symbol_check",
    "symbol_read_ports_skill",
    "parse_symbol_ports_output",
    "read_symbol_ports",
]

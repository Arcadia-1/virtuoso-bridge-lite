"""SKILL builders for Cadence Virtuoso layout editing."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from virtuoso_bridge.virtuoso.layout.editor import LayoutEditor
from virtuoso_bridge.virtuoso.layout.reader import parse_layout_geometry_output
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_bind_current_or_open_cell_view,
    close_current_cellview,
    clear_current_layout,
    layout_clear_routing,
    layout_create_polygon,
    layout_delete_selected,
    layout_delete_cell,
    layout_delete_shapes_on_layer,
    layout_fit_view,
    layout_hide_layers,
    layout_highlight_net,
    layout_create_label,
    layout_create_simple_mosaic,
    layout_select_box,
    layout_set_active_lpp,
    layout_show_layers,
    layout_show_only_layers,
    layout_create_via_by_name,
    layout_find_via_def,
    layout_create_param_inst,
    layout_create_path,
    layout_create_rect,
    layout_list_shapes,
    layout_read_geometry,
    layout_read_summary,
    layout_create_via,
    layout_via_def_expr_from_name,
)

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


class LayoutOps:
    """Attached to VirtuosoClient as ``client.layout``."""

    def __init__(self, owner: VirtuosoClient) -> None:
        self._owner = owner

    def edit(self, lib: str, cell: str, view: str = "layout",
             mode: str = "w", timeout: int = 60) -> LayoutEditor:
        """Return a LayoutEditor context manager."""
        return LayoutEditor(self._owner, lib, cell, view=view, mode=mode, timeout=timeout)


__all__ = [
    "LayoutOps",
    "LayoutEditor",
    "parse_layout_geometry_output",
    "layout_bind_current_or_open_cell_view",
    "close_current_cellview",
    "clear_current_layout",
    "layout_clear_routing",
    "layout_create_polygon",
    "layout_delete_selected",
    "layout_delete_cell",
    "layout_delete_shapes_on_layer",
    "layout_fit_view",
    "layout_hide_layers",
    "layout_highlight_net",
    "layout_create_param_inst",
    "layout_create_path",
    "layout_create_rect",
    "layout_create_label",
    "layout_create_simple_mosaic",
    "layout_select_box",
    "layout_set_active_lpp",
    "layout_show_layers",
    "layout_show_only_layers",
    "layout_create_via",
    "layout_list_shapes",
    "layout_read_geometry",
    "layout_read_summary",
    "layout_find_via_def",
    "layout_create_via_by_name",
    "layout_via_def_expr_from_name",
]

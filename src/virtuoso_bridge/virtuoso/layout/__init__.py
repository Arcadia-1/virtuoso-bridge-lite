"""SKILL builders for Cadence Virtuoso layout editing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal, TYPE_CHECKING

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
from virtuoso_bridge.virtuoso.layout.streamout import (
    GdsExportReason,
    GdsExportResult,
    export_gds,
)
from virtuoso_bridge.virtuoso.layout.xstream import (
    XStreamExportRequest,
    XStreamLogResult,
    XStreamTranslatedStructure,
    parse_xstream_log,
    xstream_export_gds_skill,
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

    def export_gds(
        self,
        library: str,
        cell: str,
        output_path: str | Path,
        *,
        stream_map: str | Path,
        view: str = "layout",
        log_path: str | Path | None = None,
        timeout: float = 300.0,
        poll_interval: float = 0.5,
        skill_timeout: float = 30.0,
        finalization_reserve: float = 30.0,
        cleanup_policy: Literal["success", "always", "never"] = "success",
        recovery_hook: Callable[[], object] | None = None,
    ) -> GdsExportResult:
        """Export one layout to GDS using XStream Out."""
        return export_gds(
            self._owner,
            library,
            cell,
            output_path,
            stream_map=stream_map,
            view=view,
            log_path=log_path,
            timeout=timeout,
            poll_interval=poll_interval,
            skill_timeout=skill_timeout,
            finalization_reserve=finalization_reserve,
            cleanup_policy=cleanup_policy,
            recovery_hook=recovery_hook,
        )


__all__ = [
    "LayoutOps",
    "LayoutEditor",
    "XStreamExportRequest",
    "XStreamTranslatedStructure",
    "XStreamLogResult",
    "GdsExportReason",
    "GdsExportResult",
    "xstream_export_gds_skill",
    "parse_xstream_log",
    "export_gds",
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

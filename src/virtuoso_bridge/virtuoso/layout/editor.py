"""High-level layout editor for declarative Virtuoso operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from virtuoso_bridge.virtuoso.ops import (
    close_current_cellview,
    save_current_cellview,
)
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_bind_current_or_open_cell_view,
    layout_create_label,
    layout_create_param_inst,
    layout_create_path,
    layout_create_polygon,
    layout_create_rect,
    layout_create_simple_mosaic,
    layout_create_via,
    layout_create_via_by_name,
    layout_delete_selected,
    layout_fit_view,
    layout_hide_layers,
    layout_highlight_net,
    layout_select_box,
    layout_set_active_lpp,
    layout_show_layers,
    layout_show_only_layers,
    layout_via_def_expr_from_name,
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
    # Legacy dict fallback
    if not response.get("ok", False):
        raise RuntimeError(f"{context} failed: {response.get('error', 'request failed')}")
    result = response.get("result", {})
    if result.get("status") != "success":
        errors = result.get("errors") or [result.get("status", "unknown failure")]
        raise RuntimeError(f"{context} failed: {errors[0]}")

class LayoutEditor:
    """Context manager for layout editing operations."""

    def __init__(
        self,
        client: VirtuosoClient,
        lib: str,
        cell: str,
        view: str = "layout",
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

    def __enter__(self) -> LayoutEditor:
        self.commands.append(
            layout_bind_current_or_open_cell_view(
                self.lib,
                self.cell,
                view=self.view,
                mode=self.mode,
            )
        )
        return self

    def add_instance(
        self,
        lib: str,
        cell: str,
        xy: tuple[float, float],
        orientation: str = "R0",
        view: str = "layout",
        name: str = "",
    ) -> None:
        """Add a layout instance."""
        self.commands.append(
            layout_create_param_inst(lib, cell, view, name, xy[0], xy[1], orientation)
        )

    def add_mosaic(
        self,
        lib: str,
        cell: str,
        *,
        origin: tuple[float, float] = (0.0, 0.0),
        orientation: str = "R0",
        rows: int,
        cols: int,
        row_pitch: float,
        col_pitch: float,
        view: str = "layout",
        view_type: str | None = None,
        name: str | None = None,
    ) -> None:
        """Add a simple mosaic from a layout master."""
        self.commands.append(
            layout_create_simple_mosaic(
                lib,
                cell,
                origin=origin,
                orientation=orientation,
                rows=rows,
                cols=cols,
                row_pitch=row_pitch,
                col_pitch=col_pitch,
                view=view,
                view_type=view_type,
                instance_name=name,
            )
        )

    def add_rect(
        self,
        layer: str,
        purpose: str,
        bbox: tuple[float, float, float, float],
    ) -> None:
        """Add a layout rectangle."""
        self.commands.append(
            layout_create_rect(layer, purpose, bbox[0], bbox[1], bbox[2], bbox[3])
        )

    def add_path(
        self,
        layer: str,
        purpose: str,
        points: Iterable[tuple[float, float]],
        width: float,
    ) -> None:
        """Add a layout path."""
        self.commands.append(layout_create_path(layer, purpose, points, width))

    def add_label(
        self,
        layer: str,
        purpose: str,
        xy: tuple[float, float],
        text: str,
        justification: str = "centerCenter",
        rotation: str = "R0",
        font: str = "roman",
        height: float = 0.1,
    ) -> None:
        """Add a layout label."""
        self.commands.append(
            layout_create_label(
                layer, purpose, xy[0], xy[1], text, justification, rotation, font, height
            )
        )

    def add_via(
        self,
        via_def_expr: str,
        xy: tuple[float, float],
        orientation: str = "R0",
        via_params_expr: str = "nil",
    ) -> None:
        """Add a layout via."""
        self.commands.append(
            layout_create_via(via_def_expr, xy[0], xy[1], orientation, via_params_expr)
        )

    def add_via_by_name(
        self,
        via_name: str,
        xy: tuple[float, float],
        orientation: str = "R0",
        via_params_expr: str = "nil",
    ) -> None:
        """Add a layout via by resolving its viaDef name from the cellview techfile."""
        self.commands.append(
            layout_create_via_by_name(
                via_name,
                xy[0],
                xy[1],
                orientation=orientation,
                via_params_expr=via_params_expr,
            )
        )

    def add_raw_via_by_name(
        self,
        via_name: str,
        xy: tuple[float, float],
        orientation: str = "R0",
        via_params_expr: str = "nil",
    ) -> None:
        """Add a layout via using the raw via API with a viaDef resolved by name."""
        self.commands.append(
            layout_create_via(
                layout_via_def_expr_from_name(via_name),
                xy[0],
                xy[1],
                orientation,
                via_params_expr,
            )
        )

    def add_polygon(
        self,
        layer: str,
        purpose: str,
        points: Iterable[tuple[float, float]],
    ) -> None:
        """Add a layout polygon."""
        self.commands.append(layout_create_polygon(layer, purpose, points))

    def fit_view(self) -> None:
        """Fit the current layout window."""
        self.commands.append(layout_fit_view())

    def set_active_lpp(self, layer: str, purpose: str = "drawing") -> None:
        """Set the active layer-purpose pair."""
        self.commands.append(layout_set_active_lpp(layer, purpose))

    def show_only_layers(self, layers: Iterable[tuple[str, str]]) -> None:
        """Hide all layers, then show the requested layer-purpose pairs."""
        self.commands.append(layout_show_only_layers(layers))

    def show_layers(self, layers: Iterable[tuple[str, str]]) -> None:
        """Show the requested layer-purpose pairs."""
        self.commands.append(layout_show_layers(layers))

    def hide_layers(self, layers: Iterable[tuple[str, str]]) -> None:
        """Hide the requested layer-purpose pairs."""
        self.commands.append(layout_hide_layers(layers))

    def highlight_net(self, net_name: str) -> None:
        """Highlight a named net in the current layout."""
        self.commands.append(
            layout_highlight_net(
                net_name,
                view=self.view,
                mode=self.mode,
            )
        )

    def select_box(
        self,
        bbox: tuple[float, float, float, float],
        *,
        mode_name: str = "replace",
    ) -> None:
        """Select figures inside a bounding box."""
        self.commands.append(
            layout_select_box(
                bbox,
                mode_name=mode_name,
                view=self.view,
                mode=self.mode,
            )
        )

    def delete_selected(self) -> None:
        """Delete the current selection."""
        self.commands.append(layout_delete_selected(view=self.view, mode=self.mode))

    def close(self) -> None:
        """Append a close-cellview operation for the current edit session."""
        self.commands.append(close_current_cellview())

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.commands.append(save_current_cellview())
            response = self.client.execute_operations(self.commands, timeout=self.timeout)
            _ensure_operation_response(response, context="layout edit")

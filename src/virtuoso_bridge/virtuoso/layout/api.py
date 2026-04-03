"""High-level layout API facade bound to a shared bridge client/session."""

from __future__ import annotations

import re
from typing import Any

from virtuoso_bridge.virtuoso.layout.editor import LayoutEditor
from virtuoso_bridge.virtuoso.layout.ops import (
    clear_current_layout,
    layout_clear_routing,
    layout_delete_cell,
    layout_delete_selected,
    layout_delete_shapes_on_layer,
    layout_fit_view,
    layout_hide_layers,
    layout_highlight_net,
    layout_list_shapes,
    layout_read_geometry,
    layout_read_summary,
    layout_select_box,
    layout_set_active_lpp,
    layout_show_layers,
    layout_show_only_layers,
)

def _decode_skill_output(raw: str) -> str:
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")

def _parse_skill_numbers(value: str) -> list[float]:
    return [float(token) for token in re.findall(r"-?\d+(?:\.\d+)?", value or "")]

def _parse_skill_point(value: str) -> tuple[float, float] | None:
    numbers = _parse_skill_numbers(value)
    if len(numbers) < 2:
        return None
    return (numbers[0], numbers[1])

def _parse_skill_point_list(value: str) -> list[tuple[float, float]] | None:
    numbers = _parse_skill_numbers(value)
    if len(numbers) < 2 or len(numbers) % 2 != 0:
        return None
    return [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)]

def parse_layout_geometry_output(raw: str) -> list[dict[str, Any]]:
    """Parse the line-oriented geometry dump returned by ``layout_read_geometry``."""
    objects: list[dict[str, Any]] = []
    for line in _decode_skill_output(raw).splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        obj: dict[str, Any] = {"kind": fields[0]}
        for field in fields[1:]:
            if "=" not in field:
                continue
            key, value = field.split("=", 1)
            obj[key] = None if value == "nil" else value
        if "bbox" in obj and isinstance(obj["bbox"], str):
            points = _parse_skill_point_list(obj["bbox"])
            obj["bbox"] = points if points and len(points) == 2 else obj["bbox"]
        if "points" in obj and isinstance(obj["points"], str):
            obj["points"] = _parse_skill_point_list(obj["points"])
        if "xy" in obj and isinstance(obj["xy"], str):
            obj["xy"] = _parse_skill_point(obj["xy"])
        objects.append(obj)
    return objects

class LayoutOps:
    """Layout operations facade backed by a shared bridge client or bridge session."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def edit(
        self,
        lib: str,
        cell: str,
        view: str = "layout",
        mode: str = "w",
        timeout: int = 60,
    ) -> LayoutEditor:
        """Return a declarative layout editor bound to the shared session."""
        return LayoutEditor(self._owner, lib, cell, view=view, mode=mode, timeout=timeout)

    def clear_current(self, timeout: int = 30) -> Any:
        """Delete all visible layout figures in the active editor."""
        return self._owner.execute_skill(clear_current_layout(), timeout=timeout)

    def read_summary(
        self,
        lib: str,
        cell: str,
        *,
        view: str = "layout",
        view_type: str | None = None,
        timeout: int = 30,
    ) -> Any:
        """Read shapes and instances summary from a layout cellview."""
        return self._owner.execute_skill(
            layout_read_summary(lib, cell, view=view, view_type=view_type),
            timeout=timeout,
        )

    def read_geometry(
        self,
        lib: str,
        cell: str,
        *,
        view: str = "layout",
        view_type: str | None = None,
        timeout: int = 30,
    ) -> Any:
        """Read detailed geometry for shapes and instances from a layout cellview."""
        response = self._owner.execute_skill(
            layout_read_geometry(lib, cell, view=view, view_type=view_type),
            timeout=timeout,
        )
        if isinstance(response, dict) and response.get("ok", False):
            result = response.get("result", {})
            if result.get("status") == "success":
                response["geometry"] = parse_layout_geometry_output(result.get("output", ""))
        return response

    def list_shapes(self, timeout: int = 15) -> Any:
        """List shape types and LPPs from the open layout window."""
        return self._owner.execute_skill(layout_list_shapes(), timeout=timeout)

    def fit_view(self, timeout: int = 15) -> Any:
        """Fit the current layout window."""
        return self._owner.execute_skill(layout_fit_view(), timeout=timeout)

    def set_active_lpp(
        self,
        layer: str,
        purpose: str = "drawing",
        *,
        timeout: int = 15,
    ) -> Any:
        """Set the active layer-purpose pair in the layout editor."""
        return self._owner.execute_skill(layout_set_active_lpp(layer, purpose), timeout=timeout)

    def show_only_layers(
        self,
        layers: list[tuple[str, str]],
        *,
        timeout: int = 15,
    ) -> Any:
        """Hide all layers, then show the requested layer-purpose pairs."""
        return self._owner.execute_skill(layout_show_only_layers(layers), timeout=timeout)

    def show_layers(
        self,
        layers: list[tuple[str, str]],
        *,
        timeout: int = 15,
    ) -> Any:
        """Show the requested layer-purpose pairs."""
        return self._owner.execute_skill(layout_show_layers(layers), timeout=timeout)

    def hide_layers(
        self,
        layers: list[tuple[str, str]],
        *,
        timeout: int = 15,
    ) -> Any:
        """Hide the requested layer-purpose pairs."""
        return self._owner.execute_skill(layout_hide_layers(layers), timeout=timeout)

    def highlight_net(self, net_name: str, timeout: int = 15) -> Any:
        """Highlight a named net in the current layout editor."""
        return self._owner.execute_skill(layout_highlight_net(net_name), timeout=timeout)

    def select_box(
        self,
        bbox: tuple[float, float, float, float],
        *,
        mode_name: str = "replace",
        timeout: int = 15,
    ) -> Any:
        """Select figures in a layout bounding box."""
        return self._owner.execute_skill(
            layout_select_box(bbox, mode_name=mode_name),
            timeout=timeout,
        )

    def delete_selected(self, timeout: int = 15) -> Any:
        """Delete the current selection from the layout editor."""
        return self._owner.execute_skill(layout_delete_selected(), timeout=timeout)

    def delete_shapes_on_layer(
        self,
        layer: str,
        purpose: str = "drawing",
        *,
        timeout: int = 30,
    ) -> Any:
        """Delete all shapes on a given layer-purpose pair from the open layout."""
        return self._owner.execute_skill(
            layout_delete_shapes_on_layer(layer, purpose),
            timeout=timeout,
        )

    def clear_routing(self, timeout: int = 30) -> Any:
        """Delete all shapes from the open layout and save it."""
        return self._owner.execute_skill(layout_clear_routing(), timeout=timeout)

    def delete_cell(self, lib: str, cell: str, timeout: int = 30) -> Any:
        """Close open windows for the cell and delete the layout cell."""
        return self._owner.execute_skill(layout_delete_cell(lib, cell), timeout=timeout)

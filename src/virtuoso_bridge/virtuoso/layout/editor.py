"""Layout editor — context manager for batch SKILL operations.

Usage:
    from virtuoso_bridge.virtuoso.layout.ops import *

    with client.layout.edit(LIB, CELL) as lay:
        lay.add(layout_create_rect("M1", "drawing", 0, 0, 1, 0.5))
        lay.add(layout_create_param_inst("tsmcN28", "nch_ulvt_mac", "layout", "M0", 0, 0, "R0"))
        lay.add(layout_create_via_by_name("M1_M2", 0.5, 0.25))
        # dbSave on exit
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from virtuoso_bridge.virtuoso.ops import (
    close_current_cellview,
    save_current_cellview,
)
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_bind_current_or_open_cell_view,
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


class LayoutEditor:
    """Context manager: open cellview → accumulate commands → save."""

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
                self.lib, self.cell, view=self.view, mode=self.mode,
            )
        )
        return self

    def add(self, skill_cmd: str) -> None:
        """Append a SKILL command to the batch."""
        self.commands.append(skill_cmd)

    def close(self) -> None:
        """Append a close-cellview operation."""
        self.commands.append(close_current_cellview())

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.commands.append(save_current_cellview())
            response = self.client.execute_operations(self.commands, timeout=self.timeout)
            _ensure_operation_response(response, context="layout edit")

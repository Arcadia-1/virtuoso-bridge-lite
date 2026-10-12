"""Layout editor — context manager for batch SKILL operations.

Usage:
    from virtuoso_bridge.virtuoso.layout.ops import *

    with client.layout.create(LIB, CELL) as lay:
        lay.add(layout_create_rect("M1", "drawing", 0, 0, 1, 0.5))
        lay.add(layout_create_param_inst("tsmcN28", "nch_ulvt_mac", "layout", "M0", 0, 0, "R0"))
        lay.add(layout_create_via_by_name("M1_M2", 0.5, 0.25))
        # dbSave on exit
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from virtuoso_bridge.virtuoso.editor import ensure_operation_response
from virtuoso_bridge.virtuoso.ops import (
    close_current_cellview,
    open_cell_view,
    save_current_cellview,
)
from virtuoso_bridge.virtuoso.basic.composition import compose_skill_script
from virtuoso_bridge.virtuoso.layout.ops import (
    LAYOUT_EDIT_CV_MARKER,
    layout_bind_current_or_open_cell_view,
)

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


class LayoutEditor:
    """Context manager: open cellview → accumulate commands → save."""

    def __init__(
        self,
        client: VirtuosoClient,
        lib: str,
        cell: str,
        view: str = "layout",
        mode: str = "a",
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
        if self.mode == "w":
            # Creation must not silently reuse an open existing cellview.
            self.commands.append(open_cell_view(self.lib, self.cell, view=self.view, mode="w"))
        else:
            self.commands.append(
                layout_bind_current_or_open_cell_view(
                    self.lib, self.cell, view=self.view, mode=self.mode,
                )
            )
        # Lets content operations such as layout_delete_shapes_on_layer() target
        # this cellview instead of the current window; cleared on exit below.
        self.commands.append(f"{LAYOUT_EDIT_CV_MARKER} = cv")
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
            batch = compose_skill_script(self.commands)
            script = f"unwindProtect({batch} {LAYOUT_EDIT_CV_MARKER} = nil)"
            response = self.client.execute_operations(
                [script], timeout=self.timeout, wrap_in_progn=False,
            )
            ensure_operation_response(response, context="layout edit")

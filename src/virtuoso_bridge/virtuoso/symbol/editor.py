"""Symbol editor — context manager for batch SKILL operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from virtuoso_bridge.virtuoso.editor import (
    EDITOR_CV,
    ensure_operation_response,
    redraw_editor_windows,
    remember_editor_cellview,
    require_editor_cellview,
    save_editor_cellview,
    wrap_editor_batch,
)
from virtuoso_bridge.virtuoso.ops import open_cell_view
from virtuoso_bridge.virtuoso.symbol.ops import symbol_check

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


class SymbolEditor:
    """Context manager: open symbol cellview → accumulate commands → check → save."""

    def __init__(
        self,
        client: VirtuosoClient,
        lib: str,
        cell: str,
        view: str = "symbol",
        view_type: str = "schematicSymbol",
        mode: str = "a",
        timeout: int = 60,
        keep_open: bool = False,
    ) -> None:
        self.client = client
        self.keep_open = keep_open
        self.lib = lib
        self.cell = cell
        self.view = view
        self.view_type = view_type
        self.mode = mode
        self.timeout = timeout
        self.commands: list[str] = []

    def __enter__(self) -> SymbolEditor:
        self.commands.append(
            open_cell_view(
                self.lib,
                self.cell,
                view=self.view,
                view_type=self.view_type,
                mode=self.mode,
            )
        )
        self.commands.append(require_editor_cellview(self.lib, self.cell, self.view))
        self.commands.append(remember_editor_cellview(opened=True))
        return self

    def add(self, skill_cmd: str) -> None:
        """Append a SKILL command to the batch."""
        self.commands.append(skill_cmd)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.commands.append(symbol_check(cv_expr=EDITOR_CV))
            self.commands.append(save_editor_cellview())
            self.commands.append(redraw_editor_windows())
            script = wrap_editor_batch(self.commands, keep_open=self.keep_open)
            response = self.client.execute_operations([script], timeout=self.timeout)
            ensure_operation_response(response, context="symbol edit")

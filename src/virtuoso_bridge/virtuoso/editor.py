"""Shared helpers for Virtuoso edit context managers."""

from __future__ import annotations

from typing import Any

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult

# The editor's own cellview handle, and whether the editor opened it (rather
# than binding to a cellview a window already holds). Commands in the batch
# may rebind ``cv``, so the close uses this handle.
EDITOR_CV = "vbEditorCv"
EDITOR_OPENED = "vbEditorOpenedCv"


def remember_editor_cellview(*, opened: bool) -> str:
    """SKILL recording the editor's cellview right after it was bound."""
    flag = "t" if opened else EDITOR_OPENED
    return f"{EDITOR_CV} = cv {EDITOR_OPENED} = {flag}"


def close_editor_cellview() -> str:
    """SKILL closing the cellview the editor opened, once it is saved.

    One dbClose balances the editor's one dbOpenCellViewByType: the edit
    lock is released, while a window that also holds the cellview keeps it.
    A cellview with unsaved changes is left open so nothing is discarded.
    """
    return (
        f"when({EDITOR_OPENED} && {EDITOR_CV} && dbValidP({EDITOR_CV}) "
        f"&& !{EDITOR_CV}~>modifiedButNotSaved dbClose({EDITOR_CV})) "
        f"{EDITOR_OPENED} = nil"
    )


def ensure_operation_response(response: Any, *, context: str) -> None:
    """Raise a consistent error when an edit batch fails."""
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

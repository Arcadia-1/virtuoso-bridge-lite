"""Shared SKILL operation builders for the virtuoso tool family."""

from __future__ import annotations

from typing import Iterable

def escape_skill_string(value: str) -> str:
    """Escape a Python string for use inside a SKILL string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')

def default_view_type_for(view: str) -> str:
    """Map a logical Virtuoso view name to the expected viewType."""
    normalized = (view or "").strip().lower()
    if normalized.startswith("layout"):
        return "maskLayout"
    if normalized == "schematic":
        return "schematic"
    return view

def skill_point(x: float, y: float) -> str:
    """Render a SKILL point literal."""
    return f"'({x:.3f} {y:.3f})"

def skill_point_list(points: Iterable[tuple[float, float]]) -> str:
    """Render a SKILL list of point literals."""
    rendered = " ".join(f"({x:.3f} {y:.3f})" for x, y in points)
    return f"'({rendered})"

def open_cell_view(
    lib: str,
    cell: str,
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "w",
) -> str:
    """Build SKILL to open and bind a target cellview to ``cv``."""
    resolved_view_type = view_type or default_view_type_for(view)
    return (
        f'cv = dbOpenCellViewByType("{escape_skill_string(lib)}" '
        f'"{escape_skill_string(cell)}" '
        f'"{escape_skill_string(view)}" '
        f'"{escape_skill_string(resolved_view_type)}" '
        f'"{escape_skill_string(mode)}")'
    )

def open_window(
    lib: str,
    cell: str,
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "a",
) -> str:
    """Build SKILL to open a Virtuoso window for a target cellview.

    Reuses an existing window if one is already open for the same
    lib/cell/view combination, focusing it instead of opening a duplicate.
    """
    elib = escape_skill_string(lib)
    ecell = escape_skill_string(cell)
    eview = escape_skill_string(view)
    resolved_view_type = view_type or default_view_type_for(view)
    evtype = escape_skill_string(resolved_view_type)
    return (
        f'let((existing) '
        f'existing = nil '
        f'foreach(w hiGetWindowList() '
        f'  let((cv) '
        f'    cv = w~>cellView '
        f'    when(cv && cv~>libName == "{elib}" '
        f'         && cv~>cellName == "{ecell}" '
        f'         && cv~>viewName == "{eview}" '
        f'      existing = w))) '
        f'if(existing '
        f'  then hiRaiseWindow(existing) window = existing '
        f'  else window = geOpen(?lib "{elib}" ?cell "{ecell}" '
        f'    ?view "{eview}" ?viewType "{evtype}" ?mode "{mode}")) '
        f'window)'
    )

def save_current_cellview() -> str:
    """Build SKILL to save the current edit cellview or bound ``cv``."""
    return (
        "let((rbCv) "
        "rbCv = if(boundp('cv) && cv then cv else geGetEditCellView()) "
        "if(rbCv then dbSave(rbCv) else nil))"
    )

def close_current_cellview() -> str:
    """Build SKILL to close the current edit cellview or bound ``cv``."""
    return (
        "let((rbCv) "
        "rbCv = if(boundp('cv) && cv then cv else geGetEditCellView()) "
        "if(rbCv then dbClose(rbCv) else nil))"
    )

def clear_current_layout() -> str:
    """Build SKILL to delete all visible figures in the active layout editor."""
    return (
        'progn('
        'pteSetAllVisible(?mode "All" ?panel "Layers") '
        'selectedFigs = geSelectAllFig() '
        'when(selectedFigs leHiDelete()) '
        'hiRedraw() '
        '"Deleted all shapes")'
    )

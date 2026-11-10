"""Shared SKILL operation builders for the virtuoso tool family."""

from __future__ import annotations

import math
from typing import Iterable

def escape_skill_string(value: str) -> str:
    """Escape a Python string for use inside a SKILL string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')

def q(value: str) -> str:
    """Wrap a Python string as a SKILL string literal (escaped + quoted).

    Shorthand for ``f'"{escape_skill_string(value)}"'`` — useful when
    composing SKILL expressions from Python f-strings, where the
    quote-then-escape pattern would otherwise be duplicated everywhere.
    """
    return f'"{escape_skill_string(value)}"'

def default_view_type_for(view: str) -> str:
    """Map a logical Virtuoso view name to the expected viewType."""
    normalized = (view or "").strip().lower()
    if normalized.startswith("layout"):
        return "maskLayout"
    if normalized == "schematic":
        return "schematic"
    if normalized == "symbol":
        return "schematicSymbol"
    if normalized == "maestro":
        return "maestro"
    return view

def _skill_number(value: float) -> str:
    """Render one finite SKILL real without silently losing grid precision."""

    number = float(value)
    if not math.isfinite(number):
        raise ValueError("SKILL coordinates must be finite")
    if number == 0:
        return "0.000"
    if math.isclose(number, round(number, 3), rel_tol=0.0, abs_tol=1e-15):
        return f"{number:.3f}"
    return format(number, ".15g")


def skill_point(x: float, y: float) -> str:
    """Render a SKILL point literal."""
    return f"'({_skill_number(x)} {_skill_number(y)})"

def skill_point_list(points: Iterable[tuple[float, float]]) -> str:
    """Render a SKILL list of point literals."""
    rendered = " ".join(
        f"({_skill_number(x)} {_skill_number(y)})" for x, y in points
    )
    return f"'({rendered})"

def open_cell_view(
    lib: str,
    cell: str,
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "a",
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
    show_hierarchy: bool = True,
) -> str:
    """Build SKILL to open a Virtuoso window for a target cellview.

    Reuses an existing window if one is already open for the same
    lib/cell/view combination, focusing it instead of opening a duplicate.

    With *show_hierarchy*, a layout window that this call opens shows every
    hierarchy level, as Shift+F does; otherwise instances may be drawn as
    empty boxes (stop level 0, the Ctrl+F state). A reused window keeps its
    own display settings.
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
        f'    ?view "{eview}" ?viewType "{evtype}" ?mode "{mode}") '
        f'{_show_layout_hierarchy("window") if show_hierarchy else ""}) '
        f'window)'
    )


def _show_layout_hierarchy(window: str) -> str:
    """SKILL setting a layout window's stop level to the full depth (Shift+F)."""
    return (
        f"when({window} && {window}~>cellView "
        f'&& {window}~>cellView~>cellViewType == "maskLayout" '
        f"if(isCallable('leSetStopLevelToMaxHierDepth) "
        f"leSetStopLevelToMaxHierDepth({window}) {window}~>stopLevel = 31) "
        f"hiRedraw({window})) "
    )

def _unsaved_action(unsaved: str) -> str:
    """Handle the caller's ``vbUnsaved`` list without a confirmation dialog."""
    if unsaved == "refuse":
        return (
            'error(sprintf(nil "unsaved cellviews would open a Save Changes dialog; '
            'save them or pass unsaved=save: %s" '
            'buildString(mapcar(lambda((x) sprintf(nil "%s/%s/%s" '
            'x~>libName x~>cellName x~>viewName)) vbUnsaved) " ")))'
        )
    if unsaved == "save":
        return (
            "foreach(x vbUnsaved unless(dbSave(x) && !dbIsCellViewModified(x) "
            'error(sprintf(nil "cannot save %s/%s/%s" x~>libName x~>cellName x~>viewName))))'
        )
    if unsaved == "discard":
        # Purging may invalidate another ID in this snapshot. It also clears
        # undo checkpoints; callers must explicitly choose this policy.
        return (
            "foreach(x vbUnsaved when(dbIsId(x) let((vbName) "
            'vbName = sprintf(nil "%s/%s/%s" x~>libName x~>cellName x~>viewName) '
            'unless(dbPurge(x) error(sprintf(nil "cannot discard %s" vbName))))))'
        )
    raise ValueError('unsaved must be "refuse", "save", or "discard"')


def close_windows(
    lib: str | None = None,
    cell: str | None = None,
    view: str | None = None,
    *,
    unsaved: str = "refuse",
) -> str:
    """Build SKILL closing the cellview windows that match lib/cell/view.

    Closing the last window of a modified cellview opens a modal "Save
    Changes" dialog that blocks the CIW. With ``unsaved="refuse"`` nothing is
    closed and the SKILL errors, naming the modified cellviews; with
    ``unsaved="save"`` they are saved first. ``unsaved="discard"`` explicitly
    purges modified matching cellviews, invalidating their database handles
    and clearing undo checkpoints. A window that closing another
    one already took away (e.g. a linked Layout XL window) is skipped.
    Returns the number of windows closed. Filters left as None match all.
    """
    handle = _unsaved_action(unsaved)
    tests = "".join(
        f' && vbCv~>{attr} == "{escape_skill_string(value)}"'
        for attr, value in (("libName", lib), ("cellName", cell), ("viewName", view))
        if value is not None
    )
    return (
        "let((vbCv vbWins vbCvs vbUnsaved (vbClosed 0)) "
        "vbWins = setof(w hiGetWindowList() "
        f"(vbCv = w~>cellView){tests}) "
        "foreach(w vbWins vbCv = w~>cellView "
        "unless(memq(vbCv vbCvs) vbCvs = cons(vbCv vbCvs))) "
        "vbUnsaved = setof(x vbCvs dbIsCellViewModified(x)) "
        f"when(vbUnsaved {handle}) "
        "foreach(w vbWins "
        "when(memq(w hiGetWindowList()) && car(errset(hiCloseWindow(w) nil)) "
        "vbClosed = vbClosed + 1)) "
        "vbClosed)"
    )


def request_exit(
    *,
    display_resources: str,
    unsaved: str = "refuse",
    display_path: str | None = None,
) -> str:
    """Queue one exit, with explicit treatment of display-resource changes.

    The acknowledgement means queued, not exited. Native exit hooks still
    run and can cancel or block exit. Recheck cellviews inside the callback
    so an edit after submission cannot bypass the default refusal.
    """
    handle = _unsaved_action(unsaved)
    if display_resources not in ("save", "discard"):
        raise ValueError('display_resources must explicitly be "save" or "discard"')
    if display_resources == "save":
        if not isinstance(display_path, str) or not display_path.strip() or any(
            char in display_path for char in "\x00\r\n"
        ):
            raise ValueError("saving display resources requires a nonempty display_path")
        save_display = (
            f"unless(drDumpDrf({q(display_path)}) "
            'error("cannot save display resources; exit refused")) '
        )
    else:
        if display_path is not None:
            raise ValueError("display_path is only used when display_resources=save")
        save_display = ""
    collect = "vbUnsaved = setof(x dbGetOpenCellViews() dbIsCellViewModified(x)) "
    preflight = (
        f"let((vbUnsaved) {collect}when(vbUnsaved {handle})) "
        if unsaved == "refuse" else ""
    )
    callback = (
        "let((vbUnsaved vbOldSuppress) "
        f"{collect}when(vbUnsaved {handle}) {save_display}"
        'vbOldSuppress = envGetVal("layout" "drmSuppressSaveDialogBox") '
        "unwindProtect(progn("
        'unless(envSetVal("layout" "drmSuppressSaveDialogBox" \'boolean t) '
        'error("cannot configure display-resource exit policy")) '
        "exit()) "
        'envSetVal("layout" "drmSuppressSaveDialogBox" \'boolean vbOldSuppress)))'
    )
    return (
        f"progn({preflight}unless(hiRegTimer({q(callback)} 1) "
        'error("cannot queue Virtuoso exit")) "exit_requested")'
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

"""SKILL operation builders for layout editing."""

from __future__ import annotations

from typing import Iterable

from virtuoso_bridge.virtuoso.ops import (
    close_current_cellview,
    default_view_type_for,
    escape_skill_string,
)

from virtuoso_bridge.virtuoso.ops import clear_current_layout  # re-export

def _lpp_expr(layer: str, purpose: str) -> str:
    """Render a layer-purpose pair in the string form expected by palette APIs."""
    return f'"{escape_skill_string(layer)} {escape_skill_string(purpose)}"'

def _bbox_expr(bbox: tuple[float, float, float, float]) -> str:
    """Render a bounding box as the nested list form used by selection APIs."""
    x0, y0, x1, y1 = bbox
    return f"list(list({x0:g} {y0:g}) list({x1:g} {y1:g}))"

def _layout_get_edit_cv_expr(
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "a",
) -> str:
    """Return a SKILL expression that finds the open edit layout cellview."""
    resolved_view_type = view_type or default_view_type_for(view)
    escaped_view = escape_skill_string(view)
    escaped_view_type = escape_skill_string(resolved_view_type)
    escaped_mode = escape_skill_string(mode)
    return (
        "let((cv editCv) "
        "editCv = geGetEditCellView() "
        f"cv = if(editCv && editCv~>viewName == \"{escaped_view}\" then editCv else nil) "
        "foreach(win hiGetWindowList() "
        f"when(!cv && win~>cellView && win~>cellView~>viewName == \"{escaped_view}\" "
        "cv = dbOpenCellViewByType("
        "win~>cellView~>libName win~>cellView~>cellName "
        f"\"{escaped_view}\" \"{escaped_view_type}\" \"{escaped_mode}\"))) "
        "cv)"
    )

def layout_bind_current_or_open_cell_view(
    lib: str,
    cell: str,
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "w",
) -> str:
    """Bind ``cv`` to the active edit layout when it matches, else open it."""
    resolved_view_type = view_type or default_view_type_for(view)
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_view = escape_skill_string(view)
    escaped_view_type = escape_skill_string(resolved_view_type)
    escaped_mode = escape_skill_string(mode)
    return (
        "cv = let((rbEditCv) "
        "rbEditCv = geGetEditCellView() "
        f"if(rbEditCv && rbEditCv~>libName == \"{escaped_lib}\" "
        f"&& rbEditCv~>cellName == \"{escaped_cell}\" "
        f"&& rbEditCv~>viewName == \"{escaped_view}\" "
        "then rbEditCv "
        f"else dbOpenCellViewByType(\"{escaped_lib}\" "
        f"\"{escaped_cell}\" "
        f"\"{escaped_view}\" "
        f"\"{escaped_view_type}\" "
        f"\"{escaped_mode}\")))"
    )

def layout_create_param_inst(
    lib: str,
    cell: str,
    view: str,
    instance_name: str,
    x: float,
    y: float,
    orientation: str,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a layout param instance by master name."""
    return (
        f'dbCreateParamInstByMasterName({cv_expr} "{escape_skill_string(lib)}" '
        f'"{escape_skill_string(cell)}" "{escape_skill_string(view)}" '
        f'"{escape_skill_string(instance_name)}" '
        f"list({x:g} {y:g}) "
        f'"{escape_skill_string(orientation)}")'
    )

def layout_create_simple_mosaic(
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
    instance_name: str | None = None,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a simple mosaic from a layout master."""
    resolved_view_type = view_type or default_view_type_for(view)
    name_expr = 'nil' if instance_name is None else f'"{escape_skill_string(instance_name)}"'
    return (
        f'let((rbMaster) '
        f'rbMaster = dbOpenCellViewByType("{escape_skill_string(lib)}" '
        f'"{escape_skill_string(cell)}" '
        f'"{escape_skill_string(view)}" '
        f'"{escape_skill_string(resolved_view_type)}" "r") '
        f'dbCreateSimpleMosaic({cv_expr} rbMaster {name_expr} '
        f'{origin[0]:g}:{origin[1]:g} '
        f'"{escape_skill_string(orientation)}" '
        f'{rows:d} {cols:d} {row_pitch:g} {col_pitch:g}))'
    )

def layout_create_path(
    layer: str,
    purpose: str,
    points: Iterable[tuple[float, float]],
    width: float,
    style: str | None = None,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a layout path."""
    base = (
        f'dbCreatePath({cv_expr} list("{escape_skill_string(layer)}" "{escape_skill_string(purpose)}") '
        f"list({ ' '.join(f'list({x:g} {y:g})' for x, y in points) }) "
        f"{width:g}"
    )
    if style is not None:
        base += f' "{escape_skill_string(style)}"'
    return base + ")"

def layout_create_rect(
    layer: str,
    purpose: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a layout rectangle."""
    return (
        f'dbCreateRect({cv_expr} list("{escape_skill_string(layer)}" "{escape_skill_string(purpose)}") '
        f"list(list({x0:g} {y0:g}) list({x1:g} {y1:g})))"
    )

def layout_create_label(
    layer: str,
    purpose: str,
    x: float,
    y: float,
    text: str,
    justification: str,
    rotation: str,
    font: str,
    height: float,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a layout label."""
    return (
        f'dbCreateLabel({cv_expr} list("{escape_skill_string(layer)}" "{escape_skill_string(purpose)}") '
        f"list({x:g} {y:g}) "
        f'"{escape_skill_string(text)}" '
        f'"{escape_skill_string(justification)}" '
        f'"{escape_skill_string(rotation)}" '
        f'"{escape_skill_string(font)}" {height:g})'
    )

def layout_create_via(
    via_def_expr: str,
    x: float,
    y: float,
    orientation: str,
    via_params_expr: str,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a layout via."""
    return (
        f'dbCreateVia({cv_expr} {via_def_expr} list({x:g} {y:g}) '
        f'"{escape_skill_string(orientation)}" {via_params_expr})'
    )

def layout_find_via_def(via_name: str, *, cv_expr: str = "cv") -> str:
    """Build SKILL to resolve a via definition by name from a cellview techfile."""
    escaped_name = escape_skill_string(via_name)
    return (
        f'let((rbTechFile rbViaDef) '
        f'rbTechFile = techGetTechFile({cv_expr}) '
        f'rbViaDef = if(rbTechFile then techFindViaDefByName(rbTechFile "{escaped_name}") else nil) '
        f'rbViaDef)'
    )

def layout_create_via_by_name(
    via_name: str,
    x: float,
    y: float,
    orientation: str = "R0",
    via_params_expr: str = "nil",
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to resolve a via definition by name and create a via."""
    escaped_orientation = escape_skill_string(orientation)
    return (
        "let((rbViaDef) "
        f"rbViaDef = {layout_find_via_def(via_name, cv_expr=cv_expr)} "
        f'dbCreateVia({cv_expr} rbViaDef list({x:g} {y:g}) "{escaped_orientation}" {via_params_expr}))'
    )

def layout_via_def_expr_from_name(via_name: str, *, cv_expr: str = "cv") -> str:
    """Build a reusable SKILL expression that resolves a viaDef by name."""
    return layout_find_via_def(via_name, cv_expr=cv_expr)

def layout_create_polygon(
    layer: str,
    purpose: str,
    points: Iterable[tuple[float, float]],
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a layout polygon."""
    return (
        f'dbCreatePolygon({cv_expr} list("{escape_skill_string(layer)}" '
        f'"{escape_skill_string(purpose)}") '
        f"list({ ' '.join(f'list({x:g} {y:g})' for x, y in points) }))"
    )

def layout_fit_view() -> str:
    """Build SKILL to fit the current layout window."""
    return (
        "let((rbWin) "
        "rbWin = hiGetCurrentWindow() "
        "if(rbWin then hiZoomAbsoluteScale(rbWin 0.9) else nil))"
    )

def layout_set_active_lpp(layer: str, purpose: str = "drawing") -> str:
    """Build SKILL to set the active layout layer-purpose pair."""
    return f"pteSetActiveLpp({_lpp_expr(layer, purpose)})"

def layout_show_only_layers(layers: Iterable[tuple[str, str]]) -> str:
    """Build SKILL to hide all layers, then show the requested layer-purpose pairs."""
    commands = ['pteSetNoneVisible(?mode "All" ?panel "Layers")']
    commands.extend(
        f"pteSetVisible({_lpp_expr(layer, purpose)} t \"Layers\")" for layer, purpose in layers
    )
    return f"progn({' '.join(commands)})"

def layout_show_layers(layers: Iterable[tuple[str, str]]) -> str:
    """Build SKILL to make specific layer-purpose pairs visible."""
    commands = [
        f"pteSetVisible({_lpp_expr(layer, purpose)} t \"Layers\")" for layer, purpose in layers
    ]
    return f"progn({' '.join(commands)})" if commands else "nil"

def layout_hide_layers(layers: Iterable[tuple[str, str]]) -> str:
    """Build SKILL to hide specific layer-purpose pairs."""
    commands = [
        f"pteSetVisible({_lpp_expr(layer, purpose)} nil \"Layers\")"
        for layer, purpose in layers
    ]
    return f"progn({' '.join(commands)})" if commands else "nil"

def layout_highlight_net(
    net_name: str,
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "a",
) -> str:
    """Build SKILL to highlight a named net by finding a shape on that net."""
    cv_expr = _layout_get_edit_cv_expr(view=view, view_type=view_type, mode=mode)
    escaped_net = escape_skill_string(net_name)
    return (
        "prog((cv fig bb ctrX ctrY) "
        f"cv = {cv_expr} "
        'unless(cv return("ERROR: no layout window open")) '
        "fig = nil "
        "foreach(shape cv~>shapes "
        f'when(!fig && shape~>net && shape~>net~>name == "{escaped_net}" fig = shape)) '
        f'unless(fig return(sprintf(nil "ERROR: net not found: %s" "{escaped_net}"))) '
        "bb = fig~>bBox "
        "ctrX = (xCoord(car(bb)) + xCoord(cadr(bb))) / 2 "
        "ctrY = (yCoord(car(bb)) + yCoord(cadr(bb))) / 2 "
        "leHiUnmarkNet() "
        'leMarkNet(list(ctrX ctrY) ?startLevel 0 ?stopLevel 32 ?thickLine t) '
        f'return(sprintf(nil "highlighted net: %s" "{escaped_net}")))'
    )

def layout_select_box(
    bbox: tuple[float, float, float, float],
    *,
    mode_name: str = "replace",
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "a",
) -> str:
    """Build SKILL to select figures in a bounding box."""
    cv_expr = _layout_get_edit_cv_expr(view=view, view_type=view_type, mode=mode)
    bbox_skill = _bbox_expr(bbox)
    normalized = mode_name.strip().lower()
    if normalized == "replace":
        selection_expr = f"progn(geDeselectAllFig(cv) geSelectArea({bbox_skill}))"
    elif normalized == "add":
        selection_expr = f"geAddSelectBox(nil {bbox_skill})"
    elif normalized in {"sub", "subtract", "remove"}:
        selection_expr = f"geDeselectArea({bbox_skill})"
    else:
        raise ValueError(f"Unsupported selection mode: {mode_name}")
    return (
        "prog((cv) "
        f"cv = {cv_expr} "
        'unless(cv return("ERROR: no layout window open")) '
        f"{selection_expr} "
        'return(sprintf(nil "selected %d figure(s)" geGetSelSetCount())))'
    )

def layout_delete_selected(
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "a",
) -> str:
    """Build SKILL to delete the current selection in the layout window."""
    cv_expr = _layout_get_edit_cv_expr(view=view, view_type=view_type, mode=mode)
    return (
        "prog((cv count) "
        f"cv = {cv_expr} "
        'unless(cv return("ERROR: no layout window open")) '
        "count = geGetSelSetCount() "
        "when(count > 0 leHiDelete()) "
        'return(sprintf(nil "deleted %d selected figure(s)" count)))'
    )

def layout_read_summary(
    lib: str,
    cell: str,
    *,
    view: str = "layout",
    view_type: str | None = None,
) -> str:
    """Build SKILL to read a summary of shapes and instances from a layout cellview."""
    resolved_view_type = view_type or default_view_type_for(view)
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_view = escape_skill_string(view)
    escaped_view_type = escape_skill_string(resolved_view_type)
    return (
        "prog((cv buf bb) "
        f'cv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" "{escaped_view}" "{escaped_view_type}" "r") '
        f'unless(cv return(sprintf(nil "ERROR: cannot open %s/%s/{escaped_view}" "{escaped_lib}" "{escaped_cell}"))) '
        f'buf = sprintf(nil "Layout: %s/%s/{escaped_view}  (%d shapes  %d instances)\\n" "{escaped_lib}" "{escaped_cell}" length(cv~>shapes) length(cv~>instances)) '
        'foreach(shape cv~>shapes '
        'buf = strcat(buf sprintf(nil "  %s [%s %s]" shape~>objType car(shape~>lpp) cadr(shape~>lpp))) '
        'when(shape~>objType == "rect" '
        'bb = shape~>bBox '
        'buf = strcat(buf sprintf(nil "  (%.3f %.3f)-(%.3f %.3f)" xCoord(car(bb)) yCoord(car(bb)) xCoord(cadr(bb)) yCoord(cadr(bb))))) '
        'buf = strcat(buf "\\n")) '
        'foreach(inst cv~>instances '
        'buf = strcat(buf sprintf(nil "  inst: %s  [%s/%s]  @ (%.3f %.3f)\\n" inst~>name inst~>libName inst~>cellName xCoord(inst~>xy) yCoord(inst~>xy))) '
        "return(buf)))"
    )

def layout_read_geometry(
    lib: str,
    cell: str,
    *,
    view: str = "layout",
    view_type: str | None = None,
) -> str:
    """Build SKILL to dump layout geometry in a parseable line-oriented format."""
    resolved_view_type = view_type or default_view_type_for(view)
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_view = escape_skill_string(view)
    escaped_view_type = escape_skill_string(resolved_view_type)
    return (
        "prog((cv out) "
        f'cv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" "{escaped_view}" "{escaped_view_type}" "r") '
        f'unless(cv return(sprintf(nil "ERROR: cannot open %s/%s/{escaped_view}" "{escaped_lib}" "{escaped_cell}"))) '
        'out = "" '
        'foreach(shape cv~>shapes '
        'out = strcat(out sprintf(nil '
        '"shape\\tobjType=%s\\tlayer=%s\\tpurpose=%s\\tbbox=%L\\tpoints=%L\\txy=%L\\torient=%L\\ttext=%L\\n" '
        'shape~>objType car(shape~>lpp) cadr(shape~>lpp) shape~>bBox shape~>points shape~>xy shape~>orient shape~>theLabel))) '
        'foreach(inst cv~>instances '
        'out = strcat(out sprintf(nil '
        '"instance\\tname=%s\\tlib=%s\\tcell=%s\\tview=%s\\txy=%L\\torient=%L\\tbbox=%L\\ttransform=%L\\n" '
        'inst~>name inst~>libName inst~>cellName inst~>viewName inst~>xy inst~>orient '
        'dbTransformBBox(inst~>master~>bBox inst~>transform) inst~>transform))) '
        "return(out))"
    )

def layout_list_shapes(
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "a",
) -> str:
    """Build SKILL to list shape type and LPP from the open layout."""
    cv_expr = _layout_get_edit_cv_expr(view=view, view_type=view_type, mode=mode)
    return (
        "prog((cv out) "
        f"cv = {cv_expr} "
        'unless(cv return("ERROR: no layout window open")) '
        'out = "" '
        'foreach(shape cv~>shapes '
        'out = strcat(out sprintf(nil "%s [%s %s]\\n" shape~>objType car(shape~>lpp) cadr(shape~>lpp)))) '
        "return(out))"
    )

def layout_delete_shapes_on_layer(
    layer: str,
    purpose: str = "drawing",
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "a",
) -> str:
    """Build SKILL to delete all shapes on a given layer/purpose from the open layout."""
    cv_expr = _layout_get_edit_cv_expr(view=view, view_type=view_type, mode=mode)
    escaped_layer = escape_skill_string(layer)
    escaped_purpose = escape_skill_string(purpose)
    return (
        "prog((cv shapes count) "
        f"cv = {cv_expr} "
        'unless(cv return("ERROR: no layout window open")) '
        "shapes = cv~>shapes "
        "count = 0 "
        'foreach(shape shapes when(car(shape~>lpp) == '
        f'"{escaped_layer}" && cadr(shape~>lpp) == "{escaped_purpose}" '
        "dbDeleteObject(shape) count = count + 1)) "
        f'return(sprintf(nil "deleted %d shape(s) on %s/%s  [%s/%s]" count "{escaped_layer}" "{escaped_purpose}" cv~>libName cv~>cellName)))'
    )

def layout_clear_routing(
    *,
    view: str = "layout",
    view_type: str | None = None,
    mode: str = "a",
) -> str:
    """Build SKILL to delete all shapes from the open layout and save it."""
    cv_expr = _layout_get_edit_cv_expr(view=view, view_type=view_type, mode=mode)
    return (
        "prog((cv count) "
        f"cv = {cv_expr} "
        'unless(cv return("ERROR: no layout window open")) '
        "count = length(cv~>shapes) "
        "foreach(shape cv~>shapes dbDeleteObject(shape)) "
        "dbSave(cv) "
        'return(sprintf(nil "deleted %d shape(s) from %s/%s (instances preserved)" count cv~>libName cv~>cellName)))'
    )

def layout_delete_cell(lib: str, cell: str) -> str:
    """Build SKILL to close layout windows and delete the target cell."""
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    return (
        "let((ddcell) "
        "foreach(win hiGetWindowList() "
        "when(win~>cellView "
        f'&& win~>cellView~>libName == "{escaped_lib}" '
        f'&& win~>cellView~>cellName == "{escaped_cell}" '
        "dbSave(win~>cellView) "
        "hiCloseWindow(win))) "
        f'ddcell = ddGetObj("{escaped_lib}" "{escaped_cell}") '
        f'if(ddcell then ddDeleteObj(ddcell) sprintf(nil "deleted: %s/%s" "{escaped_lib}" "{escaped_cell}") '
        f'else sprintf(nil "ERROR: cell not found: %s/%s" "{escaped_lib}" "{escaped_cell}")))'
    )

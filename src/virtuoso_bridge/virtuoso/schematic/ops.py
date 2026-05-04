"""SKILL operation builders for schematic editing."""

from __future__ import annotations

from typing import Iterable

from virtuoso_bridge.virtuoso.ops import (
    default_view_type_for,
    escape_skill_string,
    skill_point,
    skill_point_list,
)

def schematic_create_inst(
    master_expr: str,
    instance_name: str,
    x: float,
    y: float,
    orientation: str,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a schematic instance."""
    return (
        f'dbCreateInst({cv_expr} {master_expr} "{escape_skill_string(instance_name)}" '
        f"{skill_point(x, y)} "
        f'"{escape_skill_string(orientation)}")'
    )

def schematic_create_inst_by_master_name(
    lib: str,
    cell: str,
    view: str,
    instance_name: str,
    x: float,
    y: float,
    orientation: str,
    *,
    cv_expr: str = "cv",
    view_type: str | None = None,
    mode: str = "r",
) -> str:
    """Build SKILL to open a master cellview and create a schematic instance."""
    resolved_view_type = view_type or default_view_type_for(view)
    if resolved_view_type != view:
        open_expr = (
            f'dbOpenCellViewByType("{escape_skill_string(lib)}" '
            f'"{escape_skill_string(cell)}" '
            f'"{escape_skill_string(view)}" '
            f'"{escape_skill_string(resolved_view_type)}" '
            f'"{escape_skill_string(mode)}")'
        )
    else:
        open_expr = (
            f'dbOpenCellView("{escape_skill_string(lib)}" '
            f'"{escape_skill_string(cell)}" '
            f'"{escape_skill_string(view)}")'
        )
    return (
        "let((rbMaster) "
        f"rbMaster = {open_expr} "
        f'dbCreateInst({cv_expr} rbMaster "{escape_skill_string(instance_name)}" '
        f"{skill_point(x, y)} "
        f'"{escape_skill_string(orientation)}"))'
    )

def schematic_create_wire(
    points: Iterable[tuple[float, float]],
    *,
    cv_expr: str = "cv",
    route_style: str = "route",
    route_mode: str = "full",
) -> str:
    """Build SKILL to create a schematic wire from a sequence of points."""
    return (
        f'schCreateWire({cv_expr} "{escape_skill_string(route_style)}" '
        f'"{escape_skill_string(route_mode)}" {skill_point_list(points)} 0 0 0 nil nil)'
    )

def schematic_create_wire_label(
    x: float,
    y: float,
    text: str,
    justification: str,
    rotation: str,
    *,
    cv_expr: str = "cv",
    style: str = "stick",
    height: float = 0.0625,
) -> str:
    """Build SKILL to create a schematic wire label."""
    return (
        f'schCreateWireLabel({cv_expr} nil {skill_point(x, y)} '
        f'"{escape_skill_string(text)}" '
        f'"{escape_skill_string(justification)}" '
        f'"{escape_skill_string(rotation)}" '
        f'"{escape_skill_string(style)}" {height:g} nil)'
    )

def _schematic_term_center_expr(instance_name: str, term_name: str, *, cv_expr: str = "cv") -> str:
    return (
        "let((rbInst rbTerm rbPin rbFig rbBBox rbCtr) "
        f'rbInst = car(setof(x {cv_expr}~>instances x~>name == "{escape_skill_string(instance_name)}")) '
        "unless(rbInst error(\"instance not found\")) "
        f'rbTerm = car(setof(x rbInst~>master~>terminals x~>name == "{escape_skill_string(term_name)}")) '
        "unless(rbTerm error(\"terminal not found\")) "
        "rbPin = car(rbTerm~>pins) "
        "rbFig = when(rbPin car(rbPin~>figs)) "
        "rbBBox = when(rbFig dbTransformBBox(rbFig~>bBox rbInst~>transform)) "
        "rbCtr = when(rbBBox "
        "list((xCoord(car(rbBBox)) + xCoord(cadr(rbBBox))) / 2.0 "
        "(yCoord(car(rbBBox)) + yCoord(cadr(rbBBox))) / 2.0)) "
        "rbCtr)"
    )

def _schematic_bind_instance_and_term_expr(
    instance_name: str,
    term_name: str,
    *,
    cv_expr: str = "cv",
) -> str:
    escaped_instance = escape_skill_string(instance_name)
    escaped_term = escape_skill_string(term_name)
    return (
        f'rbInst = car(setof(x {cv_expr}~>instances x~>name == "{escaped_instance}")) '
        'unless(rbInst error("instance not found")) '
        f'rbTerm = car(setof(x rbInst~>master~>terminals x~>name == "{escaped_term}")) '
        'unless(rbTerm error("terminal not found")) '
        "rbPin = when(rbTerm car(rbTerm~>pins)) "
        "rbFig = when(rbPin car(rbPin~>figs)) "
    )

def _schematic_mos_stub_end_expr(
    normalized_term_name: str,
    *,
    extension_length: float,
) -> str:
    escaped_term = escape_skill_string(normalized_term_name.strip().upper())
    return (
        "rbMasterName = when(rbInst lowerCase(rbInst~>master~>cellName)) "
        f'rbTermName = "{escaped_term}" '
        'rbIsMos = rbMasterName && (rexMatchp("nch" rbMasterName) || rexMatchp("nmos" rbMasterName) || rexMatchp("pch" rbMasterName) || rexMatchp("pmos" rbMasterName)) '
        'rbIsPmos = rbMasterName && (rexMatchp("pch" rbMasterName) || rexMatchp("pmos" rbMasterName)) '
        "rbOrigin = when(rbIsMos dbTransformPoint(list(0 0) rbInst~>transform)) "
        "rbLocalDir = when(rbIsMos "
        f'cond((rbTermName == "G" list(-{extension_length:g} 0)) '
        f'     (rbTermName == "D" if(rbIsPmos list(0 -{extension_length:g}) list(0 {extension_length:g}))) '
        f'     (rbTermName == "B" list({extension_length:g} 0)) '
        f'     (rbTermName == "S" if(rbIsPmos list(0 {extension_length:g}) list(0 -{extension_length:g}))) '
        "     (t nil))) "
        "rbDirPt = when(rbLocalDir dbTransformPoint(rbLocalDir rbInst~>transform)) "
        "rbStubEnd = when(rbCtr && rbOrigin && rbDirPt "
        "list(xCoord(rbCtr) + (xCoord(rbDirPt) - xCoord(rbOrigin)) "
        "     yCoord(rbCtr) + (yCoord(rbDirPt) - yCoord(rbOrigin)))) "
    )

def _schematic_geometric_stub_end_expr(*, extension_length: float) -> str:
    return (
        "rbInstBBox = when(rbInst dbTransformBBox(rbInst~>master~>bBox rbInst~>transform)) "
        "rbInstCtr = when(rbInstBBox "
        "list((xCoord(car(rbInstBBox)) + xCoord(cadr(rbInstBBox))) / 2.0 "
        "(yCoord(car(rbInstBBox)) + yCoord(cadr(rbInstBBox))) / 2.0)) "
        "rbDx = when(rbCtr && rbInstCtr xCoord(rbCtr) - xCoord(rbInstCtr)) "
        "rbDy = when(rbCtr && rbInstCtr yCoord(rbCtr) - yCoord(rbInstCtr)) "
        "rbStubEnd = if(rbStubEnd rbStubEnd when(rbCtr && rbInstCtr "
        f"if(abs(rbDx) >= abs(rbDy) list(xCoord(rbCtr) + if(rbDx >= 0 {extension_length:g} -{extension_length:g}) yCoord(rbCtr)) "
        f"list(xCoord(rbCtr) yCoord(rbCtr) + if(rbDy >= 0 {extension_length:g} -{extension_length:g}))))) "
    )

_LABEL_TERM_COSMETIC_PRESETS = {
    # Original defaults (kept as the default to avoid changing existing
    # schematics' visual snapshot).
    "default": dict(extension_length=0.25, justification="centerCenter"),
    # Empirically nicer for both R0 and R90 stubs: label text sits adjacent
    # to the wire on a consistent side (top for horizontal, left for
    # vertical) instead of overlapping the stub. Opt in via
    # `cosmetic="clean"`.
    "clean":   dict(extension_length=0.5,  justification="lowerCenter"),
}


def schematic_label_instance_term(
    instance_name: str,
    term_name: str,
    net_name: str,
    *,
    cv_expr: str = "cv",
    justification: str | None = None,
    rotation: str = "R0",
    style: str = "stick",
    height: float = 0.0625,
    extension_length: float | None = None,
    cosmetic: str = "default",
    auto_rotation: bool = False,
) -> str:
    """Build SKILL to place a labeled wire stub at an instance terminal.

    Cosmetic presets (``cosmetic`` kwarg, opt-in via "clean"):
      - ``"default"`` (kept for back-compat): ``extension_length=0.25``,
        ``justification="centerCenter"``. The label glyph overlaps the
        stub's drawn wire on a typical 0.0625 µm font.
      - ``"clean"`` (recommended for new code): ``extension_length=0.5``,
        ``justification="lowerCenter"``. Label sits on a consistent side
        of the wire (top for horizontal, left for vertical).

    Explicit ``justification`` / ``extension_length`` override the preset.

    ``auto_rotation``: when True, ignore the ``rotation`` argument and pick
    R0 (label aligns with horizontal stub) vs R90 (vertical stub) based on
    the geometric stub direction (the same ``rbDx`` / ``rbDy`` already
    computed inside the SKILL). Default False keeps the legacy explicit
    behavior.
    """
    preset = _LABEL_TERM_COSMETIC_PRESETS.get(cosmetic, _LABEL_TERM_COSMETIC_PRESETS["default"])
    eff_just = justification if justification is not None else preset["justification"]
    eff_ext = extension_length if extension_length is not None else preset["extension_length"]

    # `rbDx`/`rbDy` are bound in `_schematic_geometric_stub_end_expr`;
    # auto-rotation uses them when available, falls back to the explicit
    # rotation arg if MOS-stub path was taken (rbDx/rbDy can be nil there).
    if auto_rotation:
        rotation_expr = (
            f'if(rbDx && rbDy '
            f'  if(abs(rbDx) >= abs(rbDy) "R0" "R90") '
            f'  "{escape_skill_string(rotation)}")'
        )
    else:
        rotation_expr = f'"{escape_skill_string(rotation)}"'

    return (
        "let((rbInst rbTerm rbPin rbFig rbLocalBBox rbLocalCtr rbLocalEnd rbCtr rbStubEnd rbMid "
        "rbInstBBox rbInstCtr rbDx rbDy rbMasterName rbTermName rbIsMos rbIsPmos rbOrigin rbLocalDir rbDirPt) "
        f"{_schematic_bind_instance_and_term_expr(instance_name, term_name, cv_expr=cv_expr)}"
        "rbLocalBBox = when(rbFig rbFig~>bBox) "
        "rbLocalCtr = when(rbLocalBBox "
        "list((xCoord(car(rbLocalBBox)) + xCoord(cadr(rbLocalBBox))) / 2.0 "
        "(yCoord(car(rbLocalBBox)) + yCoord(cadr(rbLocalBBox))) / 2.0)) "
        "rbCtr = when(rbLocalCtr dbTransformPoint(rbLocalCtr rbInst~>transform)) "
        f"{_schematic_mos_stub_end_expr(term_name, extension_length=eff_ext)}"
        f"{_schematic_geometric_stub_end_expr(extension_length=eff_ext)}"
        "rbMid = when(rbCtr && rbStubEnd "
        "list((xCoord(rbCtr) + xCoord(rbStubEnd)) / 2.0 "
        "(yCoord(rbCtr) + yCoord(rbStubEnd)) / 2.0)) "
        "when(rbCtr && rbStubEnd schCreateWire(cv \"route\" \"full\" list(rbCtr rbStubEnd) 0 0 0 nil nil)) "
        "when(rbMid "
        f'schCreateWireLabel({cv_expr} nil rbMid "{escape_skill_string(net_name)}" '
        f'"{escape_skill_string(eff_just)}" '
        f'{rotation_expr} '
        f'"{escape_skill_string(style)}" {height:g} nil)))'
    )


_BRANCH_DIR_OFFSETS = {
    "up":    (0.0,  1.0),
    "down":  (0.0, -1.0),
    "left":  (-1.0, 0.0),
    "right": (1.0,  0.0),
}


def schematic_label_instance_term_offset(
    instance_name: str,
    term_name: str,
    net_name: str,
    *,
    cv_expr: str = "cv",
    justification: str = "centerLeft",
    rotation: str = "R0",
    style: str = "stick",
    height: float = 0.0625,
    extension_length: float = 0.5,
    branch_length: float = 0.25,
    branch_direction: str = "up",
    auto_rotation: bool = False,
) -> str:
    """Build SKILL to place a labeled wire on a perpendicular branch off
    the main instance-terminal stub.

    Use case: label visually disjoint from the main stub but still
    electrically connected. Hand-rolling this risks placing the label
    off-wire (visually sits outside but isn't tied to the net); this
    helper emits the canonical two-wire pattern atomically:

      terminal --[main stub, length=extension_length]-- stub_end
                                                          |
                                          [branch, length=branch_length, branch_direction]
                                                          |
                                                       branch_end --label

    ``branch_direction`` is in world coordinates: "up"/"down"/"left"/
    "right". Pick whichever clears existing geometry around the
    instance.

    The branch wire is electrically continuous with the main stub
    (single ``schCreateWire`` segment chain), so the label net binds to
    the terminal correctly.
    """
    if branch_direction not in _BRANCH_DIR_OFFSETS:
        raise ValueError(
            f"branch_direction must be one of {sorted(_BRANCH_DIR_OFFSETS)}, "
            f"got {branch_direction!r}"
        )
    bdx, bdy = _BRANCH_DIR_OFFSETS[branch_direction]
    branch_dx = bdx * branch_length
    branch_dy = bdy * branch_length

    if auto_rotation:
        # For an offset label, rotation should align with the BRANCH
        # direction, not the main stub: branch is horizontal -> R0,
        # branch is vertical -> R90.
        if branch_direction in ("left", "right"):
            rotation = "R0"
        else:
            rotation = "R90"

    return (
        "let((rbInst rbTerm rbPin rbFig rbLocalBBox rbLocalCtr rbLocalEnd rbCtr rbStubEnd rbBranchEnd "
        "rbInstBBox rbInstCtr rbDx rbDy rbMasterName rbTermName rbIsMos rbIsPmos rbOrigin rbLocalDir rbDirPt) "
        f"{_schematic_bind_instance_and_term_expr(instance_name, term_name, cv_expr=cv_expr)}"
        "rbLocalBBox = when(rbFig rbFig~>bBox) "
        "rbLocalCtr = when(rbLocalBBox "
        "list((xCoord(car(rbLocalBBox)) + xCoord(cadr(rbLocalBBox))) / 2.0 "
        "(yCoord(car(rbLocalBBox)) + yCoord(cadr(rbLocalBBox))) / 2.0)) "
        "rbCtr = when(rbLocalCtr dbTransformPoint(rbLocalCtr rbInst~>transform)) "
        f"{_schematic_mos_stub_end_expr(term_name, extension_length=extension_length)}"
        f"{_schematic_geometric_stub_end_expr(extension_length=extension_length)}"
        "rbBranchEnd = when(rbStubEnd "
        f"list(xCoord(rbStubEnd) + {branch_dx:g} yCoord(rbStubEnd) + {branch_dy:g})) "
        # Single wire with three points so the branch is electrically
        # continuous with the main stub:
        "when(rbCtr && rbStubEnd && rbBranchEnd "
        "schCreateWire(cv \"route\" \"full\" list(rbCtr rbStubEnd rbBranchEnd) 0 0 0 nil nil)) "
        "when(rbBranchEnd "
        f'schCreateWireLabel({cv_expr} nil rbBranchEnd "{escape_skill_string(net_name)}" '
        f'"{escape_skill_string(justification)}" '
        f'"{escape_skill_string(rotation)}" '
        f'"{escape_skill_string(style)}" {height:g} nil)))'
    )

_PIN_MASTER_CELL = {"input": "ipin", "output": "opin", "inputOutput": "iopin"}

def _pin_master_expr(direction: str) -> str:
    cell = _PIN_MASTER_CELL.get(direction, "iopin")
    return f'dbOpenCellViewByType("basic" "{cell}" "symbol")'

def schematic_create_pin(
    pin_name: str,
    x: float,
    y: float,
    orientation: str,
    *,
    cv_expr: str = "cv",
    direction: str = "inputOutput",
) -> str:
    """Build SKILL to create a schematic pin."""
    return (
        f'schCreatePin({cv_expr} {_pin_master_expr(direction)} "{escape_skill_string(pin_name)}" '
        f'"{escape_skill_string(direction)}" nil {skill_point(x, y)} '
        f'"{escape_skill_string(orientation)}")'
    )

def schematic_create_pin_at_instance_term(
    instance_name: str,
    term_name: str,
    pin_name: str,
    *,
    cv_expr: str = "cv",
    direction: str = "inputOutput",
    orientation: str = "R0",
) -> str:
    """Build SKILL to create a schematic pin at an instance terminal center."""
    return (
        "let((rbCtr) "
        f"rbCtr = {_schematic_term_center_expr(instance_name, term_name, cv_expr=cv_expr)} "
        "when(rbCtr "
        f'schCreatePin({cv_expr} {_pin_master_expr(direction)} "{escape_skill_string(pin_name)}" '
        f'"{escape_skill_string(direction)}" nil rbCtr '
        f'"{escape_skill_string(orientation)}")))'
    )

def schematic_create_wire_between_instance_terms(
    from_instance: str,
    from_term: str,
    to_instance: str,
    to_term: str,
    *,
    cv_expr: str = "cv",
    route_style: str = "route",
    route_mode: str = "full",
) -> str:
    """Build SKILL to wire two instance terminals directly."""
    return (
        "let((rbCtrA rbCtrB) "
        f"rbCtrA = {_schematic_term_center_expr(from_instance, from_term, cv_expr=cv_expr)} "
        f"rbCtrB = {_schematic_term_center_expr(to_instance, to_term, cv_expr=cv_expr)} "
        "when(rbCtrA && rbCtrB "
        f'schCreateWire({cv_expr} "{escape_skill_string(route_style)}" '
        f'"{escape_skill_string(route_mode)}" list(rbCtrA rbCtrB) 0 0 0 nil nil)))'
    )

def schematic_check(*, cv_expr: str = "cv") -> str:
    """Build SKILL to run schematic checking."""
    return f"schCheck({cv_expr})"

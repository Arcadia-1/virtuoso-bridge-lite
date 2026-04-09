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

def schematic_label_instance_term(
    instance_name: str,
    term_name: str,
    net_name: str,
    *,
    cv_expr: str = "cv",
    justification: str = "centerCenter",
    rotation: str = "R0",
    style: str = "stick",
    height: float = 0.0625,
    extension_length: float = 0.25,
) -> str:
    """Build SKILL to place a labeled wire stub at an instance terminal."""
    return (
        "let((rbInst rbTerm rbPin rbFig rbLocalBBox rbLocalCtr rbLocalEnd rbCtr rbStubEnd rbMid "
        "rbInstBBox rbInstCtr rbDx rbDy rbMasterName rbTermName rbIsMos rbIsPmos rbOrigin rbLocalDir rbDirPt) "
        f"{_schematic_bind_instance_and_term_expr(instance_name, term_name, cv_expr=cv_expr)}"
        "rbLocalBBox = when(rbFig rbFig~>bBox) "
        "rbLocalCtr = when(rbLocalBBox "
        "list((xCoord(car(rbLocalBBox)) + xCoord(cadr(rbLocalBBox))) / 2.0 "
        "(yCoord(car(rbLocalBBox)) + yCoord(cadr(rbLocalBBox))) / 2.0)) "
        "rbCtr = when(rbLocalCtr dbTransformPoint(rbLocalCtr rbInst~>transform)) "
        f"{_schematic_mos_stub_end_expr(term_name, extension_length=extension_length)}"
        f"{_schematic_geometric_stub_end_expr(extension_length=extension_length)}"
        "rbMid = when(rbCtr && rbStubEnd "
        "list((xCoord(rbCtr) + xCoord(rbStubEnd)) / 2.0 "
        "(yCoord(rbCtr) + yCoord(rbStubEnd)) / 2.0)) "
        "when(rbCtr && rbStubEnd schCreateWire(cv \"route\" \"full\" list(rbCtr rbStubEnd) 0 0 0 nil nil)) "
        "when(rbMid "
        f'schCreateWireLabel({cv_expr} nil rbMid "{escape_skill_string(net_name)}" '
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

def schematic_set_cdf_param(
    lib: str,
    cell: str,
    inst: str,
    param: str,
    value: str,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to update a CDF parameter if the instance and parameter exist."""
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_inst = escape_skill_string(inst)
    escaped_param = escape_skill_string(param)
    escaped_value = escape_skill_string(value)
    return (
        "let((cv i cdfId p) "
        f'cv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" "schematic" nil "a") '
        f'i = car(setof(x {cv_expr}~>instances x~>name == "{escaped_inst}")) '
        "when(i "
        "cdfId = cdfGetInstCDF(i) "
        "when(cdfId "
        f'p = cdfFindParamByName(cdfId "{escaped_param}") '
        "when(p "
        f'p~>value = "{escaped_value}")))) '
        f"schCheck({cv_expr}) "
        f"dbSave({cv_expr}))"
    )

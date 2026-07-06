"""SKILL operation builders for symbol editing."""

from __future__ import annotations

from typing import Iterable

from virtuoso_bridge.virtuoso.ops import (
    escape_skill_string,
    skill_point,
    skill_point_list,
)


def _lpp_expr(layer: str, purpose: str) -> str:
    """Render a layer-purpose pair."""
    return f'list("{escape_skill_string(layer)}" "{escape_skill_string(purpose)}")'


def _bbox_expr(x0: float, y0: float, x1: float, y1: float) -> str:
    """Render a bounding box as a nested SKILL list."""
    return f"list(list({x0:g} {y0:g}) list({x1:g} {y1:g}))"


def _string_list_expr(values: Iterable[str]) -> str:
    """Render a list of SKILL string literals."""
    rendered = " ".join(f'"{escape_skill_string(value)}"' for value in values)
    return f"list({rendered})"


def symbol_create_line(
    layer: str,
    purpose: str,
    points: Iterable[tuple[float, float]],
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a symbol line."""
    return f"dbCreateLine({cv_expr} {_lpp_expr(layer, purpose)} {skill_point_list(points)})"


def symbol_create_rect(
    layer: str,
    purpose: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a symbol rectangle."""
    return f"dbCreateRect({cv_expr} {_lpp_expr(layer, purpose)} {_bbox_expr(x0, y0, x1, y1)})"


def symbol_create_polygon(
    layer: str,
    purpose: str,
    points: Iterable[tuple[float, float]],
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a symbol polygon."""
    return f"dbCreatePolygon({cv_expr} {_lpp_expr(layer, purpose)} {skill_point_list(points)})"


def symbol_create_ellipse(
    layer: str,
    purpose: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to create a symbol ellipse."""
    return f"dbCreateEllipse({cv_expr} {_lpp_expr(layer, purpose)} {_bbox_expr(x0, y0, x1, y1)})"


def symbol_create_label(
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
    label_type: str | None = None,
) -> str:
    """Build SKILL to create a symbol label."""
    create_label = (
        f"dbCreateLabel({cv_expr} {_lpp_expr(layer, purpose)} {skill_point(x, y)} "
        f'"{escape_skill_string(text)}" '
        f'"{escape_skill_string(justification)}" '
        f'"{escape_skill_string(rotation)}" '
        f'"{escape_skill_string(font)}" {height:g})'
    )
    if label_type is None:
        return create_label
    return (
        "let((rbLabel) "
        f"rbLabel = {create_label} "
        f'when(rbLabel rbLabel~>labelType = "{escape_skill_string(label_type)}") '
        "rbLabel)"
    )


def symbol_create_pin(
    pin_name: str,
    x: float,
    y: float,
    *,
    direction: str = "inputOutput",
    half_size: float = 0.0625,
    cv_expr: str = "cv",
    label: bool = True,
    label_x: float | None = None,
    label_y: float | None = None,
    label_justification: str = "centerLeft",
    label_rotation: str = "R0",
    label_font: str = "stick",
    label_height: float = 0.0625,
) -> str:
    """Build SKILL to create a symbol terminal pin.

    The pin is represented as a terminal/net pair plus a small rectangle on
    ``pin/drawing``. An optional text label is placed on the same layer.
    """
    escaped_pin = escape_skill_string(pin_name)
    escaped_direction = escape_skill_string(direction)
    x0 = x - half_size
    y0 = y - half_size
    x1 = x + half_size
    y1 = y + half_size
    effective_label_x = x if label_x is None else label_x
    effective_label_y = y if label_y is None else label_y

    label_expr = ""
    label_decl = ""
    if label:
        label_decl = " rbLabel"
        label_expr = (
            f"rbLabel = dbCreateLabel({cv_expr} {_lpp_expr('pin', 'drawing')} "
            f"{skill_point(effective_label_x, effective_label_y)} "
            f'"{escaped_pin}" '
            f'"{escape_skill_string(label_justification)}" '
            f'"{escape_skill_string(label_rotation)}" '
            f'"{escape_skill_string(label_font)}" {label_height:g}) '
            'unless(rbLabel error("pin label not created")) '
        )

    return (
        f"let((rbExistingTerm rbNet rbTerm rbRect rbPin{label_decl}) "
        f'rbExistingTerm = car(setof(x {cv_expr}~>terminals x~>name == "{escaped_pin}")) '
        'when(rbExistingTerm error("terminal already exists")) '
        f'rbNet = car(setof(x {cv_expr}~>nets x~>name == "{escaped_pin}")) '
        f'unless(rbNet rbNet = dbCreateNet({cv_expr} "{escaped_pin}")) '
        'unless(rbNet error("net not found")) '
        f'rbTerm = dbCreateTerm(rbNet "{escaped_pin}" "{escaped_direction}") '
        'unless(rbTerm error("term not created")) '
        f"rbRect = dbCreateRect({cv_expr} {_lpp_expr('pin', 'drawing')} {_bbox_expr(x0, y0, x1, y1)}) "
        'unless(rbRect error("pin rectangle not created")) '
        f'rbPin = dbCreatePin(rbNet rbRect "{escaped_pin}" rbTerm) '
        'unless(rbPin error("pin not created")) '
        f"{label_expr}"
        "rbPin)"
    )


def symbol_set_term_order(term_names: Iterable[str], *, cv_expr: str = "cv") -> str:
    """Build SKILL to set the symbol terminal order."""
    return f"{cv_expr}~>termOrder = {_string_list_expr(term_names)}"


def symbol_check(*, cv_expr: str = "cv") -> str:
    """Build SKILL to run symbol/schematic checking."""
    return f"schCheck({cv_expr})"

"""Window-independent OA group and graphical figGroup SKILL builders.

Object expressions are trusted SKILL, like ``cv_expr`` in the geometry builders.
Use object variables in one batch, not printed db IDs from an earlier session.
"""

from __future__ import annotations

import math
from typing import Literal

from virtuoso_bridge.virtuoso.layout.ops import _layout_read_cellview
from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.skill_output import parse_sexpr

GroupKind = Literal["figGroup", "group"]
_ORIENTATIONS = {"R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"}


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("group name must be a nonempty string without NUL")
    return f'"{escape_skill_string(value)}"'


def _kind(kind: GroupKind) -> str:
    if kind not in ("figGroup", "group"):
        raise ValueError("kind must be 'figGroup' or 'group'")
    return kind


def _point(point: tuple[float, float]) -> str:
    if len(point) != 2 or any(isinstance(n, bool) or not math.isfinite(n) for n in point):
        raise ValueError("point must contain two finite coordinates")
    return f"list({point[0]:.17g} {point[1]:.17g})"


def _orientation(value: str) -> str:
    if value not in _ORIENTATIONS:
        raise ValueError("unsupported OA orientation")
    return f'"{value}"'


def _lookup(name: str, kind: GroupKind, cv_expr: str) -> str:
    function = "dbGetFigGroupByName" if _kind(kind) == "figGroup" else "dbGetGroupByName"
    return f"{function}({cv_expr} {_name(name)})"


def _edit_guard(cv_expr: str) -> str:
    return (
        f'unless(dbIsId({cv_expr}) error("invalid layout cellview")) '
        f'unless({cv_expr}~>cellViewType == "maskLayout" error("not a layout cellview")) '
        f'unless(member({cv_expr}~>mode list("a" "w")) error("layout is not editable")) '
    )


def layout_create_fig_group(
    name: str, *, ordered: bool = False, origin: tuple[float, float] = (0.0, 0.0),
    orientation: str = "R0", cv_expr: str = "cv",
) -> str:
    """Create an empty figGroup; duplicate names fail without replacing anything."""
    return (
        "let((rbGroup) " + _edit_guard(cv_expr)
        + f'unless(!{_lookup(name, "figGroup", cv_expr)} error("figGroup already exists")) '
        + f'rbGroup = dbCreateFigGroup({cv_expr} {_name(name)} {"t" if ordered else "nil"} '
        + f'{_point(origin)} {_orientation(orientation)}) '
        + 'unless(rbGroup error("figGroup creation failed")) rbGroup)'
    )


def layout_create_group(name: str, *, ordered: bool = False, cv_expr: str = "cv") -> str:
    """Create an unrestricted logical set, not a graphical move/selection group."""
    return (
        "let((rbGroup) " + _edit_guard(cv_expr)
        + f'unless(!{_lookup(name, "group", cv_expr)} error("group already exists")) '
        + f'rbGroup = dbCreateGroup({cv_expr} {_name(name)} '
        + f'list("set" "{"ordered" if ordered else "unordered"}" "uniqueName" "deleteNever")) '
        + 'unless(rbGroup error("group creation failed")) rbGroup)'
    )


def _membership(name: str, object_expr: str, kind: GroupKind, cv_expr: str, *, add: bool) -> str:
    graphical = _kind(kind) == "figGroup"
    function = (
        ("dbAddFigToFigGroup" if add else "dbRemoveFigFromFigGroup") if graphical
        else ("dbAddObjectToGroup" if add else "dbDeleteObjectFromGroup")
    )
    members = "rbGroup~>figs" if graphical else "rbGroup~>members~>object"
    extra = (
        'unless(rbObject~>isShape || rbObject~>isAnyInst || '
        'member(rbObject~>objType list("stdVia" "customVia" "figGroup")) '
        'error("object is not a supported layout figure")) '
        'when(rbObject~>objType == "figGroup" error("nested figGroup mutation is not supported")) '
        'when(rbObject~>figGroup && rbObject~>figGroup != rbGroup '
        'error("figure belongs to another figGroup")) '
    ) if graphical and add else ""
    # One object per call: preflight all conditions before the single mutation.
    condition = "!" if add else ""
    return (
        "let((rbGroup rbObject) " + _edit_guard(cv_expr)
        + f'rbGroup = {_lookup(name, kind, cv_expr)} '
        + 'unless(rbGroup error("group not found")) '
        + f'rbObject = {object_expr} '
        + 'unless(dbIsId(rbObject) error("invalid group member")) '
        + f'unless(rbObject~>cellView == {cv_expr} error("cross-cellview membership refused")) '
        + 'when(rbObject == rbGroup error("self membership refused")) '
        + extra
        + f'when({condition}member(rbObject {members}) '
        + f'unless({function}(rbGroup rbObject) error("group membership change failed"))) '
        + "rbGroup)"
    )


def layout_add_to_group(
    name: str, object_expr: str, *, kind: GroupKind = "figGroup", cv_expr: str = "cv",
) -> str:
    """Add one same-cellview object; an existing membership is a no-op."""
    return _membership(name, object_expr, kind, cv_expr, add=True)


def layout_remove_from_group(
    name: str, object_expr: str, *, kind: GroupKind = "figGroup", cv_expr: str = "cv",
) -> str:
    """Remove membership, never the figure; absent membership is a no-op."""
    return _membership(name, object_expr, kind, cv_expr, add=False)


def layout_transform_fig_group(
    name: str, *, offset: tuple[float, float] = (0.0, 0.0),
    orientation: str = "R0", cv_expr: str = "cv",
) -> str:
    """Apply a relative transform about cellview (0,0), then translate.

No magnification, cross-cellview copy, or implicit group-centre pivot.
"""
    return (
        "let((rbGroup) " + _edit_guard(cv_expr)
        + f'rbGroup = {_lookup(name, "figGroup", cv_expr)} '
        + 'unless(rbGroup error("figGroup not found")) '
        + 'when(rbGroup~>figGroup error("transform the top-level figGroup instead")) '
        + f'unless(dbMoveFig(rbGroup nil list({_point(offset)} {_orientation(orientation)} 1.0)) '
        + 'error("figGroup transform failed")) rbGroup)'
    )


def layout_ungroup(name: str, *, kind: GroupKind = "figGroup", cv_expr: str = "cv") -> str:
    """Delete only the named grouping object; retain all member objects.

Nested figGroups are not recursively dissolved. Missing group is an error.
"""
    return (
        "let((rbGroup) " + _edit_guard(cv_expr)
        + f'rbGroup = {_lookup(name, kind, cv_expr)} '
        + 'unless(rbGroup error("group not found")) '
        + 'unless(dbDeleteObject(rbGroup) error("ungroup failed")) t)'
    )


def layout_read_groups(lib: str, cell: str, *, view: str = "layout") -> str:
    """Read both group kinds and member descriptors without opening a GUI.

The serialized S-expression contains no persistent database handles.
"""
    body = _group_rows() + 'sprintf(nil "%L" reverse(rows))'
    return _layout_read_cellview(lib, cell, view, "maskLayout", "rows members", body)


def _group_rows() -> str:
    return (
        'rows = nil '
        'foreach(kind list("figGroup" "group") '
        'foreach(g if(kind == "figGroup" then cv~>figGroups else cv~>groups) '
        'members = nil '
        'foreach(obj if(kind == "figGroup" then g~>figs else g~>members~>object) '
        'members = cons(list(obj~>objType car(errset(obj~>name nil)) '
        'car(errset(obj~>bBox nil)) car(errset(obj~>lpp nil))) members)) '
        'rows = cons(list(kind g~>name car(errset(g~>bBox nil)) '
        'g~>ordered reverse(members)) rows))) '
    )


def parse_layout_groups_output(raw: str) -> list[dict]:
    """Decode ``layout_read_groups`` output, including quoted Bridge responses."""
    value = parse_sexpr(raw)
    if isinstance(value, str):
        value = parse_sexpr(value)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("invalid layout group output")
    groups = []
    for row in value:
        if not isinstance(row, list) or len(row) != 5 or row[0] not in ("figGroup", "group"):
            raise ValueError("invalid layout group row")
        kind, name, bbox, ordered, members = row
        parsed_members = []
        for member in members or []:
            if not isinstance(member, list) or len(member) != 4:
                raise ValueError("invalid layout group member")
            parsed_members.append(dict(obj_type=member[0], name=member[1],
                                       bbox=_numeric_bbox(member[2]), lpp=member[3]))
        groups.append(dict(kind=kind, name=name, bbox=_numeric_bbox(bbox),
                           ordered=bool(ordered), members=parsed_members))
    return groups


def _numeric_bbox(value):
    if value is None:
        return None
    try:
        if len(value) != 2 or any(len(point) != 2 for point in value):
            raise ValueError
        return [[float(n) for n in point] for point in value]
    except (ValueError, TypeError):
        raise ValueError("invalid group bounding box") from None

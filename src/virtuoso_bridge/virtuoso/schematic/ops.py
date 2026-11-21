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
    route_style: str | None = None,
    route_mode: str = "full",
) -> str:
    """Route two endpoints by default; draw every vertex of a longer polyline.

    Explicit ``route`` accepts exactly two points: Cadence ignores later ones.
    Use ``draw`` to preserve a polyline. ``route_mode`` is ignored by ``draw``.
    A non-default mode with a polyline requires an explicit style, so flight
    lines are not silently replaced with conductive wires.
    """
    points = list(points)
    if route_style == "route" and len(points) > 2:
        raise ValueError("route accepts only two points; use route_style='draw' for a polyline")
    if route_style is None and len(points) > 2 and route_mode != "full":
        raise ValueError("polyline with non-default route_mode requires an explicit route_style")
    resolved_style = ("draw" if len(points) > 2 else "route") if route_style is None else route_style
    return (
        f'schCreateWire({cv_expr} "{escape_skill_string(resolved_style)}" '
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
    wire_expr: str = "nil",
    style: str = "stick",
    height: float = 0.0625,
) -> str:
    """Build SKILL to create a schematic wire label on the specified wire or by position."""
    return (
        f'schCreateWireLabel({cv_expr} {wire_expr} {skill_point(x, y)} '
        f'"{escape_skill_string(text)}" '
        f'"{escape_skill_string(justification)}" '
        f'"{escape_skill_string(rotation)}" '
        f'"{escape_skill_string(style)}" {height:g} nil)'
    )


_NET_STUB_DIR_OFFSETS = {
    "up": (0.0, 1.0),
    "down": (0.0, -1.0),
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
}

# Default wiring convention: a label sits at the terminal end of its wire,
# never at the far end or the midpoint. Horizontal wires carry the text
# above the line, reading away from the terminal; vertical wires carry it
# rotated R90 on the left of the line. Offsets scale with the label height
# (0.025 along / 0.04375 across at the usual 0.0625).
_LABEL_ALONG = 0.4
_LABEL_ACROSS = 0.7


def pin_anchored_label_placement(
    x: float,
    y: float,
    direction: str,
    *,
    height: float = 0.0625,
) -> tuple[float, float, str, str]:
    """Return ``(x, y, justification, rotation)`` for a label on a wire that
    leaves the point ``(x, y)`` in ``direction`` (up/down/left/right),
    following the default convention."""
    if direction not in _NET_STUB_DIR_OFFSETS:
        raise ValueError(
            f"direction must be one of {sorted(_NET_STUB_DIR_OFFSETS)}, got {direction!r}"
        )
    along = _LABEL_ALONG * height
    across = _LABEL_ACROSS * height
    if direction == "right":
        return x + along, y + across, "lowerLeft", "R0"
    if direction == "left":
        return x - along, y + across, "lowerRight", "R0"
    if direction == "up":
        return x - across, y + along, "lowerLeft", "R90"
    return x - across, y - along, "lowerRight", "R90"


def _pin_anchored_label_expr(start: str, end: str, *, height: float) -> str:
    """SKILL binding rbLblPt / rbLblJust / rbLblRot for a label at *start* on
    the wire from *start* toward *end* (both SKILL point expressions), as
    :func:`pin_anchored_label_placement` places it."""
    along = _LABEL_ALONG * height
    across = _LABEL_ACROSS * height
    return (
        f"rbLblDx = when({start} && {end} xCoord({end}) - xCoord({start})) "
        f"rbLblDy = when({start} && {end} yCoord({end}) - yCoord({start})) "
        "when(rbLblDx && rbLblDy "
        "cond((abs(rbLblDx) >= abs(rbLblDy) && rbLblDx >= 0 "
        f'rbLblPt = list(xCoord({start}) + {along:g} yCoord({start}) + {across:g}) rbLblJust = "lowerLeft" rbLblRot = "R0") '
        "(abs(rbLblDx) >= abs(rbLblDy) "
        f'rbLblPt = list(xCoord({start}) - {along:g} yCoord({start}) + {across:g}) rbLblJust = "lowerRight" rbLblRot = "R0") '
        "(rbLblDy >= 0 "
        f'rbLblPt = list(xCoord({start}) - {across:g} yCoord({start}) + {along:g}) rbLblJust = "lowerLeft" rbLblRot = "R90") '
        "(t "
        f'rbLblPt = list(xCoord({start}) - {across:g} yCoord({start}) - {along:g}) rbLblJust = "lowerRight" rbLblRot = "R90"))) '
    )


def schematic_create_net_stub(
    net_name: str,
    x: float,
    y: float,
    *,
    direction: str = "right",
    length: float | None = None,
    cv_expr: str = "cv",
    route_style: str = "route",
    route_mode: str = "full",
    justification: str | None = None,
    rotation: str | None = None,
    style: str = "stick",
    height: float = 0.0625,
    label_anchor: str = "start",
) -> str:
    """Build SKILL to draw a short named net stub.

    This is the generic version of the common schematic pattern: draw a small
    wire segment and attach a wire label to it so matching labels connect
    electrically without long crossing wires.

    ``label_anchor="start"`` (default) puts the label at ``(x, y)``, above a
    horizontal stub or left of a vertical one, as
    :func:`pin_anchored_label_placement` describes. ``"midpoint"`` keeps the
    older centred label (``centerCenter`` unless ``justification`` is given).
    The default stub length is 0.25 for start and 0.5 for midpoint.
    """
    if direction not in _NET_STUB_DIR_OFFSETS:
        raise ValueError(
            f"direction must be one of {sorted(_NET_STUB_DIR_OFFSETS)}, got {direction!r}"
        )
    if label_anchor not in {"start", "midpoint"}:
        raise ValueError(f"label_anchor must be 'start' or 'midpoint', got {label_anchor!r}")
    if length is None:
        length = 0.25 if label_anchor == "start" else 0.5
    if length <= 0:
        raise ValueError("length must be positive")
    dx, dy = _NET_STUB_DIR_OFFSETS[direction]
    end_x = x + dx * length
    end_y = y + dy * length
    if label_anchor == "start":
        label_x, label_y, auto_just, auto_rot = pin_anchored_label_placement(
            x, y, direction, height=height
        )
    else:
        label_x = (x + end_x) / 2.0
        label_y = (y + end_y) / 2.0
        auto_just = "centerCenter"
        auto_rot = "R90" if direction in {"up", "down"} else "R0"
    justification = justification or auto_just
    label_rotation = rotation or auto_rot
    wire = schematic_create_wire(
        [(x, y), (end_x, end_y)], cv_expr=cv_expr, route_style=route_style, route_mode=route_mode
    )
    label = schematic_create_wire_label(
        label_x, label_y, net_name, justification, label_rotation, cv_expr=cv_expr,
        wire_expr="rbWireObj" if label_anchor == "start" else "nil", style=style, height=height,
    )
    if label_anchor == "midpoint":
        return f"progn({wire} {label})"
    return (
        f"let((rbWire rbWireObj) rbWire = {wire} "
        "rbWireObj = if(listp(rbWire) car(rbWire) rbWire) "
        f"when(rbWireObj {label}))"
    )


def schematic_create_net_expression(
    net_name: str,
    net_expression: str,
    x: float,
    y: float,
    *,
    cv_expr: str = "cv",
    justification: str = "lowerLeft",
    rotation: str = "R0",
    font_style: str = "stick",
    height: float = 0.0625,
) -> str:
    """Build SKILL to attach an inherited-connection expression to a net wire.

    Cadence inherited connections are modeled in two parts: the lower-level
    schematic gets a net-expression label with ``schCreateNetExpression``, and
    each upper-level instance can override that expression through a ``netSet``
    property.
    """
    escaped_net = escape_skill_string(net_name)
    return (
        "let((rbWire rbLabel) "
        f"rbWire = car(setof(x {cv_expr}~>shapes "
        'x~>lpp && car(x~>lpp) == "wire" && '
        f'x~>net && x~>net~>name == "{escaped_net}")) '
        'unless(rbWire error("wire for net not found")) '
        f"rbLabel = schCreateNetExpression({cv_expr} "
        f'"{escape_skill_string(net_expression)}" rbWire {skill_point(x, y)} '
        f'"{escape_skill_string(justification)}" '
        f'"{escape_skill_string(rotation)}" '
        f'"{escape_skill_string(font_style)}" {height:g}) '
        "rbLabel)"
    )

def schematic_set_netset_property(
    instance_name: str,
    property_name: str,
    net_name: str,
    *,
    cv_expr: str = "cv",
) -> str:
    """Build SKILL to set an instance inherited-connection override."""
    return (
        "let((rbInst) "
        f'rbInst = car(setof(x {cv_expr}~>instances x~>name == "{escape_skill_string(instance_name)}")) '
        'unless(rbInst error("instance not found")) '
        f'dbReplaceProp(rbInst "{escape_skill_string(property_name)}" '
        f'"netSet" "{escape_skill_string(net_name)}"))'
    )

def _schematic_find_term_expr(term_name: str, aliases: tuple[str, ...] = ()) -> str:
    """SKILL evaluating to rbInst's master terminal named *term_name*, or nil.

    An exact name wins; otherwise the name and then each alias are matched
    case-insensitively, since PDKs differ (``D`` in one, ``d`` in another).
    """
    candidates = " ".join(
        f'"{escape_skill_string(name)}"' for name in dict.fromkeys(n.upper() for n in (term_name, *aliases))
    )
    return (
        "let((rbTerms rbFound) rbTerms = rbInst~>master~>terminals "
        f'rbFound = car(setof(x rbTerms x~>name == "{escape_skill_string(term_name)}")) '
        f"foreach(rbAlias list({candidates}) "
        "unless(rbFound rbFound = car(setof(x rbTerms upperCase(x~>name) == rbAlias)))) "
        "rbFound)"
    )


def _schematic_term_missing_error(term_name: str) -> str:
    return (
        f'error("terminal %s not found on %s/%s; its terminals: %L" "{escape_skill_string(term_name)}" '
        "rbInst~>master~>libName rbInst~>master~>cellName rbInst~>master~>terminals~>name)"
    )


def _schematic_term_center_expr(instance_name: str, term_name: str, *, cv_expr: str = "cv") -> str:
    return (
        "let((rbInst rbTerm rbPin rbFig rbBBox rbCtr) "
        f'rbInst = car(setof(x {cv_expr}~>instances x~>name == "{escape_skill_string(instance_name)}")) '
        "unless(rbInst error(\"instance not found\")) "
        f"rbTerm = {_schematic_find_term_expr(term_name)} "
        f"unless(rbTerm {_schematic_term_missing_error(term_name)}) "
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
    aliases: tuple[str, ...] = (),
    missing_ok: bool = False,
) -> str:
    escaped_instance = escape_skill_string(instance_name)
    # With missing_ok an absent terminal leaves rbTerm nil, and every later
    # step of the caller is guarded by when(), so nothing is drawn.
    missing = "" if missing_ok else f"unless(rbTerm {_schematic_term_missing_error(term_name)}) "
    return (
        f'rbInst = car(setof(x {cv_expr}~>instances x~>name == "{escaped_instance}")) '
        'unless(rbInst error("instance not found")) '
        f"rbTerm = {_schematic_find_term_expr(term_name, aliases)} "
        f"{missing}"
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
    # Default wiring convention: 0.25 stub, label at the terminal end, above
    # a horizontal stub or left of a vertical one (see
    # pin_anchored_label_placement). Justification and rotation follow the
    # stub direction.
    "default": dict(extension_length=0.25, justification=None, anchor="pin"),
    # The former default: label centred on the stub midpoint, overlapping
    # the wire. Kept for reproducing older schematics.
    "legacy":  dict(extension_length=0.25, justification="centerCenter", anchor="midpoint"),
    # Midpoint label beside a longer stub.
    "clean":   dict(extension_length=0.5,  justification="lowerCenter", anchor="midpoint"),
}


def schematic_label_instance_term(
    instance_name: str,
    term_name: str,
    net_name: str,
    *,
    cv_expr: str = "cv",
    justification: str | None = None,
    rotation: str | None = None,
    style: str = "stick",
    height: float = 0.0625,
    extension_length: float | None = None,
    cosmetic: str = "default",
    auto_rotation: bool = False,
    bind_label_to_wire: bool | None = None,
    aliases: tuple[str, ...] = (),
    missing_ok: bool = False,
) -> str:
    """Build SKILL to place a labeled wire stub at an instance terminal.

    ``term_name`` matches the master's terminal exactly, else
    case-insensitively, then each of ``aliases`` case-insensitively. A
    terminal that is still not found raises a SKILL error naming the
    master's terminals, unless ``missing_ok`` is set: then nothing is drawn.

    Cosmetic presets (``cosmetic`` kwarg):
      - ``"default"``: the house wiring convention. A 0.25 stub leaves the
        terminal outward; the label stands at the terminal end of the stub,
        above it when horizontal (``lowerLeft``/``lowerRight``, reading away
        from the terminal) and rotated R90 on its left when vertical.
      - ``"legacy"``: the former default, ``extension_length=0.25`` and a
        ``centerCenter`` label on the stub midpoint.
      - ``"clean"``: ``extension_length=0.5``, ``lowerCenter`` label on the
        stub midpoint.

    Explicit ``justification`` / ``rotation`` / ``extension_length``
    override the preset.

    ``auto_rotation`` (midpoint presets): ignore ``rotation`` and pick R0 vs
    R90 from the geometric stub direction. The default preset always picks
    its rotation from the stub direction unless ``rotation`` is given.

    ``bind_label_to_wire``: when True, pass the created wire object to
    ``schCreateWireLabel`` instead of ``nil``. This avoids unconnected-label
    warnings in flows that check the generated schematic immediately. By default
    pin-anchored labels bind to their wire and midpoint presets use position.

    When two terminals of the same net face each other, connect them with
    :func:`schematic_wire_instance_terms_labeled` (one wire, one label)
    instead of labelling each terminal.
    """
    if cosmetic not in _LABEL_TERM_COSMETIC_PRESETS:
        raise ValueError(
            f"cosmetic must be one of {sorted(_LABEL_TERM_COSMETIC_PRESETS)}, got {cosmetic!r}"
        )
    preset = _LABEL_TERM_COSMETIC_PRESETS[cosmetic]
    pin_anchor = preset["anchor"] == "pin"
    if bind_label_to_wire is None:
        bind_label_to_wire = pin_anchor
    eff_ext = extension_length if extension_length is not None else preset["extension_length"]

    if pin_anchor:
        point_expr = "rbLblPt"
        just_expr = (
            f'"{escape_skill_string(justification)}"' if justification is not None else "rbLblJust"
        )
        rotation_expr = f'"{escape_skill_string(rotation)}"' if rotation is not None else "rbLblRot"
        label_setup = _pin_anchored_label_expr("rbCtr", "rbStubEnd", height=height)
    else:
        point_expr = "rbMid"
        eff_just = justification if justification is not None else preset["justification"]
        just_expr = f'"{escape_skill_string(eff_just)}"'
        explicit_rotation = rotation or "R0"
        label_setup = ""
        # `rbDx`/`rbDy` are bound in `_schematic_geometric_stub_end_expr`;
        # auto-rotation uses them when available, falls back to the explicit
        # rotation arg if MOS-stub path was taken (rbDx/rbDy can be nil there).
        if auto_rotation:
            rotation_expr = (
                f'if(rbDx && rbDy '
                f'  if(abs(rbDx) >= abs(rbDy) "R0" "R90") '
                f'  "{escape_skill_string(explicit_rotation)}")'
            )
        else:
            rotation_expr = f'"{escape_skill_string(explicit_rotation)}"'

    wire_vars = "rbWire rbWireObj " if bind_label_to_wire else ""
    wire_expr = (
        f'rbWire = when(rbCtr && rbStubEnd schCreateWire({cv_expr} "route" "full" list(rbCtr rbStubEnd) 0 0 0 nil nil)) '
        "rbWireObj = if(listp(rbWire) car(rbWire) rbWire) "
        if bind_label_to_wire
        else f'when(rbCtr && rbStubEnd schCreateWire({cv_expr} "route" "full" list(rbCtr rbStubEnd) 0 0 0 nil nil)) '
    )
    label_wire_expr = "rbWireObj" if bind_label_to_wire else "nil"
    label_guard_expr = f"rbWireObj && {point_expr}" if bind_label_to_wire else point_expr
    label_vars = "rbLblDx rbLblDy rbLblPt rbLblJust rbLblRot " if pin_anchor else ""

    return (
        f"let((rbInst rbTerm rbPin rbFig rbLocalBBox rbLocalCtr rbLocalEnd rbCtr rbStubEnd rbMid {wire_vars}{label_vars}"
        "rbInstBBox rbInstCtr rbDx rbDy rbMasterName rbTermName rbIsMos rbIsPmos rbOrigin rbLocalDir rbDirPt) "
        f"{_schematic_bind_instance_and_term_expr(instance_name, term_name, cv_expr=cv_expr, aliases=aliases, missing_ok=missing_ok)}"
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
        f"{label_setup}"
        f"{wire_expr}"
        f"when({label_guard_expr} "
        f'schCreateWireLabel({cv_expr} {label_wire_expr} {point_expr} "{escape_skill_string(net_name)}" '
        f"{just_expr} "
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
        f'schCreateWire({cv_expr} "draw" "full" list(rbCtr rbStubEnd rbBranchEnd) 0 0 0 nil nil)) '
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

def schematic_wire_instance_terms_labeled(
    from_instance: str,
    from_term: str,
    to_instance: str,
    to_term: str,
    net_name: str,
    *,
    cv_expr: str = "cv",
    route_style: str = "route",
    route_mode: str = "full",
    style: str = "stick",
    height: float = 0.0625,
) -> str:
    """Build SKILL to wire two terminals of one net and label the wire once.

    The default convention for terminals that face each other, such as one
    stage's output and the next stage's input in a chain: a single wire
    between the terminal centres, with one label at the *from* end placed as
    :func:`pin_anchored_label_placement` places it. Labelling both terminals
    separately would print the net name twice on a short wire.
    """
    return (
        "let((rbCtrA rbCtrB rbWire rbWireObj rbLblDx rbLblDy rbLblPt rbLblJust rbLblRot) "
        f"rbCtrA = {_schematic_term_center_expr(from_instance, from_term, cv_expr=cv_expr)} "
        f"rbCtrB = {_schematic_term_center_expr(to_instance, to_term, cv_expr=cv_expr)} "
        f"{_pin_anchored_label_expr('rbCtrA', 'rbCtrB', height=height)}"
        "when(rbCtrA && rbCtrB "
        f'rbWire = schCreateWire({cv_expr} "{escape_skill_string(route_style)}" '
        f'"{escape_skill_string(route_mode)}" list(rbCtrA rbCtrB) 0 0 0 nil nil) '
        "rbWireObj = if(listp(rbWire) car(rbWire) rbWire)) "
        "when(rbWireObj && rbLblPt "
        f'schCreateWireLabel({cv_expr} rbWireObj rbLblPt "{escape_skill_string(net_name)}" '
        f'rbLblJust rbLblRot "{escape_skill_string(style)}" {height:g} nil)))'
    )

def schematic_check(*, cv_expr: str = "cv") -> str:
    """Build SKILL to run schematic checking."""
    return f"schCheck({cv_expr})"

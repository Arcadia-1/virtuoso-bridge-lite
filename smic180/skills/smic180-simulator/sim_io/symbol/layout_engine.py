"""Symbol layout engine — pure Python redesign + SKILL generation.

Step 2: Parse extraction data, classify pins, calculate new body + positions.
Step 3: Generate a single SKILL script that applies the entire layout.

No SKILL calls, no network — all pure computation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────

class Side(Enum):
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


# ── Data structures (extraction) ──────────────────────────────

@dataclass(frozen=True)
class RectData:
    layer: str
    purpose: str
    left: float
    bottom: float
    right: float
    top: float

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def cy(self) -> float:
        return (self.bottom + self.top) / 2.0


@dataclass(frozen=True)
class LineData:
    layer: str
    purpose: str
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class LabelData:
    layer: str
    purpose: str
    text: str
    x: float
    y: float


@dataclass(frozen=True)
class TermData:
    index: int
    pin_index: int
    name: str
    direction: str
    cx: float
    cy: float
    fig_layer: str = "pin"
    fig_purpose: str = "drawing"


@dataclass
class SymbolInfo:
    rects: list[RectData] = field(default_factory=list)
    lines: list[LineData] = field(default_factory=list)
    labels: list[LabelData] = field(default_factory=list)
    terminals: list[TermData] = field(default_factory=list)


# ── Data structures (layout result) ───────────────────────────

@dataclass
class LayoutConfig:
    pin_pitch: float = 0.4
    wire_length: float = 0.375
    end_margin: float = 0.5
    label_inset: float = 0.125
    core_label_inset: float = 0.3
    center_x: float = 2.5
    center_y: float = -0.5
    body_width: float = 5.0
    min_body_half: float = 0.125


@dataclass(frozen=True)
class PinLayout:
    term_index: int
    pin_index: int
    name: str
    direction: str
    side: Side
    new_cx: float
    new_cy: float
    wire_x1: float = 0.0
    wire_y1: float = 0.0
    wire_x2: float = 0.0
    wire_y2: float = 0.0
    label_x: float = 0.0
    label_y: float = 0.0
    label_orig_x: float = 0.0
    label_orig_y: float = 0.0
    is_core: bool = False
    needs_duplicate_fig: bool = False


@dataclass(frozen=True)
class BodyLayout:
    left: float
    bottom: float
    right: float
    top: float


@dataclass
class LayoutResult:
    body: BodyLayout
    pins: list[PinLayout] = field(default_factory=list)


# ── Parsing ────────────────────────────────────────────────────

def parse_symbol_info(raw_output: str) -> SymbolInfo:
    """Parse pipe-delimited output from extractSymbolInfo SKILL.

    Handles SKILL string escaping: output may be wrapped in quotes with
    ``\\n`` escape sequences instead of real newlines.
    """
    info = SymbolInfo()

    # SKILL string literal: strip surrounding quotes, unescape \n
    data = raw_output.strip()
    if data.startswith('"') and data.endswith('"'):
        data = data[1:-1]
    data = data.replace('\\n', '\n')

    for line in data.split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if not parts:
            continue
        kind = parts[0]
        try:
            if kind == "RECT" and len(parts) == 7:
                info.rects.append(RectData(
                    layer=parts[1], purpose=parts[2],
                    left=float(parts[3]), bottom=float(parts[4]),
                    right=float(parts[5]), top=float(parts[6]),
                ))
            elif kind == "LINE" and len(parts) == 7:
                info.lines.append(LineData(
                    layer=parts[1], purpose=parts[2],
                    x1=float(parts[3]), y1=float(parts[4]),
                    x2=float(parts[5]), y2=float(parts[6]),
                ))
            elif kind == "LABEL" and len(parts) == 6:
                info.labels.append(LabelData(
                    layer=parts[1], purpose=parts[2],
                    text=parts[3],
                    x=float(parts[4]), y=float(parts[5]),
                ))
            elif kind == "TERM" and len(parts) >= 7:
                fig_layer = parts[7] if len(parts) > 7 else "pin"
                fig_purpose = parts[8] if len(parts) > 8 else "drawing"
                info.terminals.append(TermData(
                    index=int(parts[1]), pin_index=int(parts[2]),
                    name=parts[3], direction=parts[4],
                    cx=float(parts[5]), cy=float(parts[6]),
                    fig_layer=fig_layer, fig_purpose=fig_purpose,
                ))
        except (ValueError, IndexError) as e:
            logger.warning("[layout_engine] Skipping malformed terminal line: %s (%s)", line.strip(), e)
            continue
    return info


# ── Layout Engine ──────────────────────────────────────────────

class LayoutEngine:
    """Pure-Python layout engine for symbol pin redistribution."""

    def __init__(self, config: LayoutConfig | None = None):
        self.config = config or LayoutConfig()

    def redesign(self, info: SymbolInfo) -> LayoutResult:
        classified = self._classify_pins(info)
        body = self._calc_body(classified)
        label_map = self._build_label_map(info)
        pins = self._calc_pin_layouts(classified, body, label_map)
        return LayoutResult(body=body, pins=pins)

    # ── Classify pins by side ─────────────────────────────────

    def _classify_pins(self, info: SymbolInfo) -> dict[Side, list[TermData]]:
        if not info.terminals:
            return {s: [] for s in Side}

        body_rects = [r for r in info.rects
                      if r.layer == "instance" and r.purpose == "drawing"]
        if body_rects:
            body_L = min(r.left for r in body_rects)
            body_R = max(r.right for r in body_rects)
            body_B = min(r.bottom for r in body_rects)
            body_T = max(r.top for r in body_rects)
        else:
            all_xs = [r.left for r in info.rects] + [r.right for r in info.rects]
            all_ys = [r.bottom for r in info.rects] + [r.top for r in info.rects]
            body_L = min(all_xs) if all_xs else 0.0
            body_R = max(all_xs) if all_xs else 0.0
            body_B = min(all_ys) if all_ys else 0.0
            body_T = max(all_ys) if all_ys else 0.0

        classified: dict[Side, list[TermData]] = {s: [] for s in Side}

        for term in info.terminals:
            dists = {
                Side.LEFT: abs(term.cx - body_L),
                Side.RIGHT: abs(term.cx - body_R),
                Side.TOP: abs(term.cy - body_T),
                Side.BOTTOM: abs(term.cy - body_B),
            }
            side = min(dists, key=dists.__getitem__)
            classified[side].append(term)

        for side in (Side.LEFT, Side.RIGHT):
            classified[side].sort(key=lambda t: t.cy, reverse=True)
        for side in (Side.TOP, Side.BOTTOM):
            classified[side].sort(key=lambda t: t.cx)

        return classified

    # ── Build label position map (for duplicate-safe matching) ──

    @staticmethod
    def _build_label_map(info: SymbolInfo) -> dict[str, list[tuple[float, float]]]:
        """Map label text → list of (x, y) positions from extraction."""
        label_map: dict[str, list[tuple[float, float]]] = {}
        for lbl in info.labels:
            label_map.setdefault(lbl.text, []).append((lbl.x, lbl.y))
        return label_map

    @staticmethod
    def _find_label_pos(
        label_map: dict[str, list[tuple[float, float]]],
        name: str, pin_cx: float, pin_cy: float,
    ) -> tuple[float, float]:
        """Find the label position closest to a pin's current position."""
        positions = label_map.get(name, [])
        if not positions:
            return (pin_cx, pin_cy)
        return min(positions, key=lambda p: (p[0] - pin_cx) ** 2 + (p[1] - pin_cy) ** 2)

    # ── Calculate body dimensions ─────────────────────────────

    def _calc_body(self, classified: dict[Side, list[TermData]]) -> BodyLayout:
        cfg = self.config
        _CORE_SFX = "_CORE"

        n_left = sum(
            1 for side in Side
            for t in classified.get(side, [])
            if not t.name.endswith(_CORE_SFX)
        )

        vert_span = (n_left - 1) * cfg.pin_pitch if n_left > 1 else 0.0
        body_height = max(vert_span + 2 * cfg.end_margin, 2 * cfg.min_body_half)
        body_width = cfg.body_width

        L = cfg.center_x - body_width / 2
        R = cfg.center_x + body_width / 2
        B = cfg.center_y - body_height / 2
        T = cfg.center_y + body_height / 2

        return BodyLayout(left=L, bottom=B, right=R, top=T)

    # ── Calculate pin layouts (position + wire + label) ───────

    def _calc_pin_layouts(
        self, classified: dict[Side, list[TermData]], body: BodyLayout,
        label_map: dict[str, list[tuple[float, float]]],
    ) -> list[PinLayout]:
        cfg = self.config
        _CORE_SFX = "_CORE"

        # ── Pass 0: build ordered list of non-CORE pins for left side ──
        left_group = sorted(
            [t for t in classified.get(Side.LEFT, []) if not t.name.endswith(_CORE_SFX)],
            key=lambda t: -t.cy,
        )
        right_group = sorted(
            [t for t in classified.get(Side.RIGHT, []) if not t.name.endswith(_CORE_SFX)],
            key=lambda t: -t.cy,
        )
        top_group = sorted(
            [t for t in classified.get(Side.TOP, []) if not t.name.endswith(_CORE_SFX)],
            key=lambda t: t.cx,
        )
        bottom_group = sorted(
            [t for t in classified.get(Side.BOTTOM, []) if not t.name.endswith(_CORE_SFX)],
            key=lambda t: t.cx,
        )

        ordered_left = left_group + right_group + top_group + bottom_group

        # De-duplicate by name (multi-pin terminals like VSS appear twice)
        seen_names: set[str] = set()
        unique_left: list[TermData] = []
        for t in ordered_left:
            if t.name not in seen_names:
                seen_names.add(t.name)
                unique_left.append(t)
        ordered_left = unique_left

        # Build CORE terminal lookup: base_name -> TermData
        core_terms: dict[str, TermData] = {}
        for side in Side:
            for t in classified.get(side, []):
                if t.name.endswith(_CORE_SFX):
                    base = t.name[:-len(_CORE_SFX)]
                    core_terms[base] = t

        # Label consumption tracker
        consumed: set[tuple[str, float, float]] = set()

        def claim_label(name: str, pin_cx: float, pin_cy: float) -> tuple[float, float]:
            positions = label_map.get(name, [])
            available = [(x, y) for x, y in positions
                         if (name, x, y) not in consumed]
            if not available:
                return (pin_cx, pin_cy)
            best = min(available,
                       key=lambda p: (p[0] - pin_cx) ** 2 + (p[1] - pin_cy) ** 2)
            consumed.add((name, best[0], best[1]))
            return best

        # ── Pass 1: non-CORE pins on LEFT side ──
        layouts: list[PinLayout] = []
        left_y_map: dict[str, float] = {}
        n = len(ordered_left)
        pin_span = (n - 1) * cfg.pin_pitch if n > 1 else 0.0

        for i, term in enumerate(ordered_left):
            frac = i / (n - 1) if n > 1 else 0.5
            cy = body.top - cfg.end_margin - frac * pin_span

            # Pin figure outside body on left
            pin_cx = body.left - cfg.wire_length
            pin_cy = cy

            # Wire stub from body edge to pin
            wx1, wy1 = body.left, cy
            wx2, wy2 = body.left - cfg.wire_length, cy

            # Label inside body
            lx, ly = body.left + cfg.label_inset, cy

            left_y_map[term.name] = cy
            orig_lx, orig_ly = claim_label(term.name, term.cx, term.cy)

            layouts.append(PinLayout(
                term_index=term.index, pin_index=term.pin_index,
                name=term.name, direction=term.direction, side=Side.LEFT,
                new_cx=pin_cx, new_cy=pin_cy,
                wire_x1=wx1, wire_y1=wy1,
                wire_x2=wx2, wire_y2=wy2,
                label_x=lx, label_y=ly,
                label_orig_x=orig_lx, label_orig_y=orig_ly,
            ))

        # ── Pass 2: CORE + duplicate pins on RIGHT side ──
        for term in ordered_left:
            cy = left_y_map[term.name]
            base_name = term.name

            # Pin figure outside body on right
            pin_cx = body.right + cfg.wire_length
            pin_cy = cy

            # Wire stub from body edge to pin
            wx1, wy1 = body.right, cy
            wx2, wy2 = body.right + cfg.wire_length, cy

            # Label inside body near right edge
            lx, ly = body.right - cfg.core_label_inset, cy

            if base_name in core_terms:
                # Existing CORE terminal — move its figure
                ct = core_terms[base_name]
                orig_lx, orig_ly = claim_label(ct.name, ct.cx, ct.cy)

                layouts.append(PinLayout(
                    term_index=ct.index, pin_index=ct.pin_index,
                    name=ct.name, direction=ct.direction, side=Side.RIGHT,
                    new_cx=pin_cx, new_cy=pin_cy,
                    wire_x1=wx1, wire_y1=wy1,
                    wire_x2=wx2, wire_y2=wy2,
                    label_x=lx, label_y=ly,
                    label_orig_x=orig_lx, label_orig_y=orig_ly,
                    is_core=True,
                ))
            else:
                # No CORE terminal — create duplicate pin figure
                layouts.append(PinLayout(
                    term_index=term.index, pin_index=term.pin_index,
                    name=term.name, direction=term.direction, side=Side.RIGHT,
                    new_cx=pin_cx, new_cy=pin_cy,
                    wire_x1=wx1, wire_y1=wy1,
                    wire_x2=wx2, wire_y2=wy2,
                    label_x=lx, label_y=ly,
                    label_orig_x=0.0, label_orig_y=0.0,
                    needs_duplicate_fig=True,
                ))

        return layouts


# ── SKILL Generation (Step 3) ─────────────────────────────────

def generate_apply_skill(
    lib: str, cell: str, result: LayoutResult,
    config: LayoutConfig | None = None,
) -> str:
    """Generate ONE SKILL script that applies the entire layout.

    Each statement on its own line (SMIC180 proven pattern for load() compatibility).
    Handles three pin categories:
      - Left non-CORE: move pin figure + create wire + move label
      - Right CORE: move pin figure + create wire + move label (not deleted)
      - Right duplicate: dbCreateRect + dbAddFigToPin + create wire + dbCreateLabel
    """
    cfg = config or LayoutConfig()
    body = result.body
    pins = result.pins

    lines: list[str] = []

    # Open cellview
    lines.append('let((cv term pin fig newFig bb w h lbl shapes)')
    lines.append(f'  cv = dbOpenCellViewByType("{lib}" "{cell}" "symbol" "schematicSymbol" "a")')
    lines.append(f'  unless(cv error("APPLY-LAYOUT: cannot open cellview"))')

    # Delete old body rects (instance/drawing + device/drawing for backward compat)
    lines.append('  shapes = setof(s cv~>shapes s~>objType == "rect" &&')
    lines.append('    ((s~>layerName == "instance" && s~>purpose == "drawing") ||')
    lines.append('     (s~>layerName == "device"   && s~>purpose == "drawing")))')
    lines.append('  foreach(s shapes dbDeleteObject(s))')

    # Delete old wire lines (device/drawing lines)
    lines.append('  shapes = setof(s cv~>shapes')
    lines.append('    s~>objType == "line" && s~>layerName == "device" && s~>purpose == "drawing")')
    lines.append('  foreach(s shapes dbDeleteObject(s))')

    # Create single body rect (device/drawing)
    lines.append(
        f'  dbCreateRect(cv list("device" "drawing")'
        f' list(list({body.left:g} {body.bottom:g})'
        f' list({body.right:g} {body.top:g})))')

    # Process each pin
    for pin in pins:
        tidx = pin.term_index
        pidx = pin.pin_index
        ncx = pin.new_cx
        ncy = pin.new_cy
        name = pin.name
        olx, oly = pin.label_orig_x, pin.label_orig_y

        if pin.needs_duplicate_fig:
            # ── Right-side duplicate: create new pin figure + wire + label ──
            # Create new pin rect and attach to existing pin via dbAddFigToPin
            lines.append(f'  term = nth({tidx} cv~>terminals)')
            lines.append(f'  when(term')
            lines.append(f'    pin = car(term~>pins)')
            lines.append(f'    fig = car(pin~>figs)')
            lines.append(f'    bb = fig~>bBox')
            lines.append(f'    w = car(cadr(bb)) - car(car(bb))')
            lines.append(f'    h = cadr(cadr(bb)) - cadr(car(bb))')
            lines.append(
                f'    newFig = dbCreateRect(cv list("pin" "drawing")'
                f' list(list({ncx:g} - w/2.0 {ncy:g} - h/2.0)'
                f' list({ncx:g} + w/2.0 {ncy:g} + h/2.0)))')
            lines.append(f'    dbAddFigToPin(pin newFig)')
            lines.append(f'  )')
            # Wire stub
            wx1, wy1 = pin.wire_x1, pin.wire_y1
            wx2, wy2 = pin.wire_x2, pin.wire_y2
            lines.append(
                f'  dbCreateLine(cv list("device" "drawing")'
                f' list(list({wx1:g} {wy1:g}) list({wx2:g} {wy2:g})))')
            # New label (right-aligned for right side)
            lx, ly = pin.label_x, pin.label_y
            lines.append(
                f'  dbCreateLabel(cv list("pin" "label")'
                f' list({lx:g} {ly:g}) "{name}"'
                f' "centerRight" "R0" "stick" 0.25)')

        elif pin.is_core:
            # ── Right-side CORE: move pin figure + create wire + move label ──
            lines.append(f'  term = nth({tidx} cv~>terminals)')
            lines.append(f'  when(term')
            # Move ALL pin figures for this terminal (handles multi-pin terminals)
            lines.append(f'    foreach(p term~>pins')
            lines.append(f'      fig = car(p~>figs)')
            lines.append(f'      when(fig')
            lines.append(f'        bb = fig~>bBox')
            lines.append(f'        w = car(cadr(bb)) - car(car(bb))')
            lines.append(f'        h = cadr(cadr(bb)) - cadr(car(bb))')
            lines.append(
                f'        fig~>bBox = list('
                f'list({ncx:g} - w/2.0 {ncy:g} - h/2.0)'
                f' list({ncx:g} + w/2.0 {ncy:g} + h/2.0))')
            lines.append(f'      )')
            lines.append(f'    )')
            lines.append(f'  )')
            # Wire stub
            wx1, wy1 = pin.wire_x1, pin.wire_y1
            wx2, wy2 = pin.wire_x2, pin.wire_y2
            lines.append(
                f'  dbCreateLine(cv list("device" "drawing")'
                f' list(list({wx1:g} {wy1:g}) list({wx2:g} {wy2:g})))')
            # Move label + fix justification + font size for right side
            lx, ly = pin.label_x, pin.label_y
            lines.append(
                f'  lbl = car(setof(s cv~>shapes'
                f' s~>objType == "label" && s~>theLabel == "{name}"'
                f' && abs(car(s~>xy) - {olx:g}) < 0.01'
                f' && abs(cadr(s~>xy) - {oly:g}) < 0.01))')
            lines.append(f'  when(lbl lbl~>xy = list({lx:g} {ly:g}) lbl~>justify = "centerRight" lbl~>orient = "R0" lbl~>height = 0.25)')

        else:
            # ── Left-side non-CORE: move pin figure + create wire + move label ──
            lines.append(f'  term = nth({tidx} cv~>terminals)')
            lines.append(f'  when(term')
            # Move ALL pin figures for this terminal
            lines.append(f'    foreach(p term~>pins')
            lines.append(f'      fig = car(p~>figs)')
            lines.append(f'      when(fig')
            lines.append(f'        bb = fig~>bBox')
            lines.append(f'        w = car(cadr(bb)) - car(car(bb))')
            lines.append(f'        h = cadr(cadr(bb)) - cadr(car(bb))')
            lines.append(
                f'        fig~>bBox = list('
                f'list({ncx:g} - w/2.0 {ncy:g} - h/2.0)'
                f' list({ncx:g} + w/2.0 {ncy:g} + h/2.0))')
            lines.append(f'      )')
            lines.append(f'    )')
            lines.append(f'  )')
            # Wire stub
            wx1, wy1 = pin.wire_x1, pin.wire_y1
            wx2, wy2 = pin.wire_x2, pin.wire_y2
            lines.append(
                f'  dbCreateLine(cv list("device" "drawing")'
                f' list(list({wx1:g} {wy1:g}) list({wx2:g} {wy2:g})))')
            # Move label + fix justification + font size for left side
            lx, ly = pin.label_x, pin.label_y
            lines.append(
                f'  lbl = car(setof(s cv~>shapes'
                f' s~>objType == "label" && s~>theLabel == "{name}"'
                f' && abs(car(s~>xy) - {olx:g}) < 0.01'
                f' && abs(cadr(s~>xy) - {oly:g}) < 0.01))')
            lines.append(f'  when(lbl lbl~>xy = list({lx:g} {ly:g}) lbl~>justify = "centerLeft" lbl~>orient = "R0" lbl~>height = 0.25)')

    # Save + return
    lines.append(f'  dbSave(cv)')
    n_left = sum(1 for p in pins if p.side == Side.LEFT)
    n_core = sum(1 for p in pins if p.is_core)
    n_dup = sum(1 for p in pins if p.needs_duplicate_fig)
    lines.append(f'  printf("APPLY-LAYOUT: OK  left={n_left}  core={n_core}  dup={n_dup}")')
    lines.append(f'  t')
    lines.append(f')')

    return "\n".join(lines) + "\n"

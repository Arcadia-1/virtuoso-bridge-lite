#!/usr/bin/env python3
"""Restyle labels in an imported digital cell's layout view.

Two passes in one SKILL traversal:

1. **`text/drawing` labels — layer name "text" AND purpose "drawing"**
   (Innovus signoff stamps per-instance / per-net names on this layer —
   typically 100s for a small design). Default: shrink height to
   ``0.05`` µm so they no longer dominate the visual.

2. **Pin labels** — any ``<layer>/pin`` (M1/M4/M5/M6/...; the electrical
   port markers, including the VDD/VSS labels ``add_power_labels.py``
   placed). Default: height ``0.2`` µm and font ``roman`` (legible at
   typical zoom).

Heights and font are CLI-overridable.

Prerequisites
-------------
* ``virtuoso-bridge`` daemon is up.
* Target cell already exists in the target library (post-strmin import).

Usage
-----
::

    python restyle_labels.py --target-lib DIG_OUTPUT --cell LFSR_32BIT

    # Override heights / font
    python restyle_labels.py --target-lib DIG_OUTPUT --cell LFSR_32BIT \\
        --text-height 0.04 --pin-height 0.25 --pin-font helvetica
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import q as _q


SIDE_TO_ORIENT = {
    "Top":    "R90",
    "Bottom": "R270",
    "Left":   "R180",
    "Right":  "R0",
}


def parse_floorplan_pin_sides(fp_tcl_path: str) -> dict[str, str]:
    """Parse 'editPin -side X -pin {...}' blocks from an Innovus floorplan
    Tcl file. Returns ``{pin_name: 'Top'|'Bottom'|'Left'|'Right'}``.

    Only the explicit ``-side`` form is parsed.  ``-edge N`` is skipped
    because the integer→side mapping is Innovus-version-dependent and
    unsafe to assume.  Pins missing from the returned dict will fall
    through to the caller's bbox-based heuristic.
    """
    text = Path(fp_tcl_path).read_text()
    # Strip line comments so '#' in commented-out editPin lines is ignored.
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())

    pin_to_side: dict[str, str] = {}
    # Split on editPin command boundaries; each cmd may span multiple lines
    # (continued via '\') and may contain a multi-line {...} pin block with
    # NESTED braces (e.g. '{align_dout[43]} {align_dout[42]} ...').
    cmds = re.split(r"(?=\beditPin\b)", text)
    for cmd in cmds:
        if not cmd.lstrip().startswith("editPin"):
            continue
        side_match = re.search(r"-side\s+(Top|Bottom|Left|Right)\b", cmd)
        if not side_match:
            continue   # legacy `-edge N` form: skip, fall back to heuristic
        side = side_match.group(1)
        # Balanced-brace extraction of the -pin block (regex can't do it).
        m = re.search(r"-pin\s+\{", cmd)
        if not m:
            continue
        start = m.end()    # char index just after the opening '{'
        depth = 1
        i = start
        while i < len(cmd) and depth > 0:
            c = cmd[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue       # malformed; skip silently
        block = cmd[start:i - 1]
        # Each pin: {name[idx]} (braced; brackets allowed) or bare token.
        # Convert Tcl-style 'name[i]' to Cadence layout label form 'name<i>'
        # because strmin's -replaceBusBitChar rewrites bus brackets that way
        # in the imported labels.
        for tok in re.finditer(r"\{([^{}]+)\}|(\S+)", block):
            name = (tok.group(1) or tok.group(2)).strip()
            if name and not name.startswith("-"):
                cadence_name = name.replace("[", "<").replace("]", ">")
                pin_to_side[cadence_name] = side
    return pin_to_side


def _build_orient_map_skill(pin_to_side: dict[str, str]) -> str:
    """Emit a SKILL ``makeTable`` populated with pin_name → orient strings."""
    if not pin_to_side:
        return "nil"
    pairs = " ".join(
        f"orientMap[{_q(name)}] = {_q(SIDE_TO_ORIENT[side])}"
        for name, side in pin_to_side.items()
    )
    return f"(orientMap = makeTable(\"orientMap\" nil) {pairs} orientMap)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 2)[0])
    parser.add_argument("--target-lib", required=True,
                        help="OA library containing the cell")
    parser.add_argument("--cell", required=True,
                        help="Top cell name (layout view will be opened)")
    parser.add_argument("--text-height", type=float, default=0.05,
                        help="Height for text/drawing labels in µm (default: 0.05)")
    parser.add_argument("--text-layer", default="text",
                        help="Layer name to match for the shrink pass (default: text)")
    parser.add_argument("--pin-height", type=float, default=0.2,
                        help="Height for <layer>/pin labels in µm (default: 0.2)")
    parser.add_argument("--pin-font", default="roman",
                        help="Font for <layer>/pin labels (default: roman)")
    parser.add_argument("--pin-justify", default="centerLeft",
                        help="Justification anchor for pin labels (default: centerLeft). "
                             "Cadence default is upperLeft which puts the anchor at the "
                             "top-left of the text, awkward when the text is rotated.")
    parser.add_argument("--orient-edge-pins", action="store_true", default=True,
                        help="Rotate pin labels based on which cellview edge they're nearest "
                             "(default: on). bottom=R270, top=R90, left=R180, right=R90.")
    parser.add_argument("--no-orient-edge-pins", dest="orient_edge_pins",
                        action="store_false",
                        help="Disable edge-based rotation of pin labels.")
    parser.add_argument("--edge-threshold", type=float, default=4.0,
                        help="A pin label is considered 'on edge X' only if its distance to X "
                             "is less than this many µm (default: 4.0). Power labels in the "
                             "interior typically stay above this threshold and are not rotated.")
    parser.add_argument("--floorplan-tcl", default=None,
                        help="If provided, parse 'editPin -side X -pin {...}' blocks from this "
                             "Innovus floorplan Tcl file and assign pin orient *directly* from "
                             "the -side directive (Top->R90, Bottom->R270, Left->R180, "
                             "Right->R0). Source-of-truth approach — robust against bBox-snapshot "
                             "timing issues that bite the coordinate heuristic for pins near a "
                             "corner. Pins missing from the floorplan map (e.g. VDD/VSS power "
                             "labels added later, or pins placed via legacy '-edge N') fall back "
                             "to the bbox heuristic.")
    args = parser.parse_args()

    pin_to_side = {}
    if args.floorplan_tcl:
        pin_to_side = parse_floorplan_pin_sides(args.floorplan_tcl)
        print(f"[restyle] parsed {len(pin_to_side)} pin->side mappings "
              f"from {args.floorplan_tcl}")

    client = VirtuosoClient.from_env()

    # Single SKILL traversal that classifies each label by (layerName, purpose)
    # and applies the appropriate restyle.  Returns a summary string.
    # Uses the `~>attr = val` assignment form (compiles to dbSetq), which works
    # for label height/font/orient; bare dbSet(s (quote height) ...) silently
    # no-ops in this IC release.
    do_orient = "t" if args.orient_edge_pins else "nil"
    orient_table_init = _build_orient_map_skill(pin_to_side)
    # The SKILL block's two-pass shape:
    #   Pass A) Set heights/font/justify on every label, build counts.
    #           Lookup pin_name in orientMap (if provided); if hit, set
    #           orient from map and bump n_or_map.
    #   Pass B) For pin labels NOT covered by the map, run the legacy
    #           bbox-distance heuristic. This pass reads cv~>bBox AFTER
    #           pass A so the bbox reflects final label sizes (avoids the
    #           "bbox-snapshot timing" trap that mis-rotated corner pins).
    skill = (
        f'let((cv bb xmin ymin xmax ymax thr orientMap '
        f'     n_text n_pin n_or_map n_or_heu pin_shapes) '
        f'  cv = dbOpenCellViewByType({_q(args.target_lib)} {_q(args.cell)} "layout" nil "a") '
        f'  if(cv == nil then '
        f'     "OPEN_FAILED" '
        f'   else '
        f'     orientMap = {orient_table_init} '
        f'     thr = {args.edge_threshold} '
        f'     n_text = 0  n_pin = 0  n_or_map = 0  n_or_heu = 0 '
        f'     pin_shapes = nil '
        # Pass A: heights/font/justify + map-driven orient
        f'     foreach(s cv~>shapes '
        f'       when(s~>objType == "label" '
        f'         cond( '
        f'           (s~>layerName == {_q(args.text_layer)} && s~>purpose == "drawing" '
        f'              s~>height = {args.text_height} '
        f'              n_text = n_text + 1) '
        f'           (s~>purpose == "pin" '
        f'              s~>height = {args.pin_height} '
        f'              s~>font = {_q(args.pin_font)} '
        f'              s~>justify = {_q(args.pin_justify)} '
        f'              n_pin = n_pin + 1 '
        f'              if(orientMap && orientMap[s~>theLabel] '
        f'                then '
        f'                  s~>orient = orientMap[s~>theLabel] '
        f'                  n_or_map = n_or_map + 1 '
        f'                else '
        f'                  pin_shapes = cons(s pin_shapes) '
        f'              )) '
        f'         ) '
        f'       ) '
        f'     ) '
        # Pass B: bbox heuristic fallback for pins not in map (read bBox AFTER pass A)
        f'     when({do_orient} && pin_shapes '
        f'       bb = cv~>bBox '
        f'       xmin = caar(bb)  ymin = cadar(bb) '
        f'       xmax = caadr(bb) ymax = cadadr(bb) '
        f'       foreach(s pin_shapes '
        f'         let((x y dl dr db dt mn) '
        f'           x = car(s~>xy)  y = cadr(s~>xy) '
        f'           dl = x - xmin  dr = xmax - x '
        f'           db = y - ymin  dt = ymax - y '
        f'           mn = min(min(dl dr) min(db dt)) '
        f'           when(mn < thr '
        f'             cond( '
        f'               (mn == db  s~>orient = "R270" n_or_heu = n_or_heu + 1) '
        f'               (mn == dt  s~>orient = "R90"  n_or_heu = n_or_heu + 1) '
        f'               (mn == dl  s~>orient = "R180" n_or_heu = n_or_heu + 1) '
        f'               (mn == dr  s~>orient = "R0"   n_or_heu = n_or_heu + 1) '
        f'             ) '
        f'           ) '
        f'         ) '
        f'       ) '
        f'     ) '
        f'     dbSave(cv) '
        f'     dbClose(cv) '
        f'     sprintf(nil "%s/drawing: %d h=%g | pin: %d h=%g font=%s justify=%s | orient: %d via map + %d via bbox-heuristic = %d/%d total" '
        f'             {_q(args.text_layer)} n_text {args.text_height} '
        f'             n_pin {args.pin_height} {_q(args.pin_font)} {_q(args.pin_justify)} '
        f'             n_or_map n_or_heu (n_or_map + n_or_heu) n_pin) '
        f'  ) '
        f')'
    )
    r = client.execute_skill(skill)
    out = (r.output or "").strip().strip('"')
    if out == "OPEN_FAILED":
        sys.exit(f"ERROR: could not open {args.target_lib}/{args.cell}/layout for write")
    print(f"[OK] {args.target_lib}/{args.cell}/layout: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

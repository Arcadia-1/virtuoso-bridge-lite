#!/usr/bin/env python3
"""Add VDD/VSS power labels to a routed Virtuoso layout cellview.

After ``import_gds.py`` brings a P&R'd GDS into Virtuoso, the resulting
layout has no power-net labels — GDS is pure geometry.  This script
fills them in by:

1. Scanning the top cell's instances and picking the first one whose
   master has both the power pin and the ground pin.  Any standard cell
   or filler with both pins works — no need for the user to know the
   PDK's cell names.

2. Reading the power/ground pin bbox in the master's coord system,
   transforming through the instance's xform (handles R0 / MX / MY / R180
   automatically), and dropping a label centered on each rail at the top
   cell's middle x.

Defaults match a typical TSMC-style PDK (M1.pin / VDD / VSS).  Override
via CLI flags for other PDKs (e.g. ``--power-pin VPWR --ground-pin VGND``).

Prerequisites
-------------
* ``virtuoso-bridge start`` running, daemon loaded.
* ``--target-lib`` exists in ``cds.lib`` and contains a layout cellview
  named ``--cell`` populated with std-cell instances (post-strmin).
"""

from __future__ import annotations

import argparse
import sys

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import q as _q


# Single-shot SKILL: find ref instance, read master pin y, transform to
# top cell coords, create labels, save.  Returns a status line — either
# "OK ..." or "ERROR ..." — for clean Python-side reporting.
SKILL = """\
let((cv ref pterm gterm m pf gf pbb gbb pyc gyc xform px py gx gy x_mid plab glab result fallbackLib)
  fallbackLib = {fallback_lib}
  cv = dbOpenCellViewByType({lib} {cell} "layout" nil "a")
  if(null(cv) then
    sprintf(nil "ERROR: cannot open %s/%s/layout for edit" {lib} {cell})
  else
    ;; --- 1. pick the first instance whose master has both pins ---
    ;; If the instance's own master has no terminals (strmin without LEF
    ;; preserves shapes but not pins), fall back to looking in
    ;; `fallbackLib` (typically the std-cell PDK lib).
    ref = car(setof(i cv~>instances
                      let((m)
                        m = dbOpenCellViewByType(i~>libName i~>cellName
                                                 "layout" nil "r")
                        when(m && length(m~>terminals) == 0 && fallbackLib != ""
                          let((m2)
                            m2 = dbOpenCellViewByType(fallbackLib i~>cellName
                                                       "layout" nil "r")
                            when(m2 m = m2)))
                        and(m
                            exists(tm m~>terminals tm~>name == {pwr_pin})
                            exists(tm m~>terminals tm~>name == {gnd_pin})))))
    if(null(ref) then
      sprintf(nil "ERROR: no instance has both %s and %s pins"
              {pwr_pin} {gnd_pin})
    else
      ;; --- 2. read master pin bboxes (use fallback lib if needed) ---
      m = dbOpenCellViewByType(ref~>libName ref~>cellName "layout" nil "r")
      when(m && length(m~>terminals) == 0 && fallbackLib != ""
        let((m2)
          m2 = dbOpenCellViewByType(fallbackLib ref~>cellName "layout" nil "r")
          when(m2 m = m2)))
      pterm = car(setof(tm m~>terminals tm~>name == {pwr_pin}))
      gterm = car(setof(tm m~>terminals tm~>name == {gnd_pin}))
      pf = (car pterm~>pins)~>fig
      gf = (car gterm~>pins)~>fig
      pbb = pf~>bBox
      gbb = gf~>bBox
      pyc = (cadr(car(pbb)) + cadr(cadr(pbb))) / 2.0
      gyc = (cadr(car(gbb)) + cadr(cadr(gbb))) / 2.0
      ;; --- 3. transform master pin centers to top-cell coords ---
      xform = list(ref~>xy ref~>orient 1.0)
      px = car(dbTransformPoint(list(0.0 pyc) xform))  ; (we only use y)
      py = cadr(dbTransformPoint(list(0.0 pyc) xform))
      gy = cadr(dbTransformPoint(list(0.0 gyc) xform))
      ;; --- 4. x = explicit override OR top-cell horizontal center ---
      ;; The midline default falls inside the SRAM for SRAM-left layouts
      ;; (the most common case for SRAM-bearing digital), where there's no
      ;; std-cell M1 rail to bind to.  Pass --label-x <X> to force the
      ;; label into the std-cell strip instead.
      x_mid = if({label_x_override} != 0.0
                 {label_x_override}
                 (xCoord(lowerLeft(cv~>bBox)) + xCoord(upperRight(cv~>bBox))) / 2.0)
      ;; --- 5. drop labels and save ---
      plab = dbCreateLabel(cv list({layer} {purpose}) list(x_mid py)
                           {pwr_text} "centerCenter" "R0" {font} {height})
      glab = dbCreateLabel(cv list({layer} {purpose}) list(x_mid gy)
                           {gnd_text} "centerCenter" "R0" {font} {height})
      dbSave(cv)
      result = sprintf(nil "OK ref=%s/%s@%L %s | %s@%L | %s@%L"
              ref~>libName ref~>cellName ref~>xy ref~>orient
              plab~>theLabel plab~>xy
              glab~>theLabel glab~>xy)
      dbClose(cv)
      result
    )
  )
)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--target-lib", required=True, help="OA library that contains the layout cellview")
    parser.add_argument("--cell", required=True, help="Top cell name")
    parser.add_argument("--power-pin",  default="VDD", help="Master terminal name for power (default: VDD)")
    parser.add_argument("--ground-pin", default="VSS", help="Master terminal name for ground (default: VSS)")
    parser.add_argument("--power-label",  default=None, help="Label text written for power (default: same as --power-pin)")
    parser.add_argument("--ground-label", default=None, help="Label text written for ground (default: same as --ground-pin)")
    parser.add_argument("--layer",   default="M1",   help="Label layer (default: M1)")
    parser.add_argument("--purpose", default="pin",  help="Layer purpose (default: pin)")
    parser.add_argument("--font",    default="roman", help="Label font (default: roman)")
    parser.add_argument("--height",  default=1.0, type=float, help="Label height in micron (default: 1.0)")
    parser.add_argument("--fallback-lib", default="tcbn28hpcplusbwp12t30p140",
                        help="Library to fall back to when an instance's own master has no "
                             "terminals (typical when strmin imported shapes without LEF). "
                             "Empty string disables fallback. Default: tcbn28hpcplusbwp12t30p140")
    parser.add_argument("--label-x", default=0.0, type=float,
                        help="Force label X coordinate (in micron). 0.0 (default) = use layout's "
                             "horizontal midline, which falls INSIDE the SRAM for SRAM-left "
                             "layouts. Set to a value inside the std-cell strip.")
    args = parser.parse_args()

    client = VirtuosoClient.from_env()

    skill = SKILL.format(
        lib=_q(args.target_lib),
        cell=_q(args.cell),
        pwr_pin=_q(args.power_pin),
        gnd_pin=_q(args.ground_pin),
        pwr_text=_q(args.power_label  if args.power_label  else args.power_pin),
        gnd_text=_q(args.ground_label if args.ground_label else args.ground_pin),
        layer=_q(args.layer),
        purpose=_q(args.purpose),
        font=_q(args.font),
        height=args.height,
        fallback_lib=_q(args.fallback_lib),
        label_x_override=args.label_x,
    )
    r = client.execute_skill(skill)
    if r.errors:
        sys.exit(f"SKILL error: {r.errors}")
    out = (r.output or "").strip().strip('"')
    if out.startswith("ERROR"):
        sys.exit(out)
    print(out)

    # let any open layout windows pick up the new labels
    client.execute_skill("foreach(w hiGetWindowList() hiRedraw(w)) t")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

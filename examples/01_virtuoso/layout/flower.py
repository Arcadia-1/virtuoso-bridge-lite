#!/usr/bin/env python3
"""Draw a flower in Virtuoso layout using polygons.

Usage::

    python flower.py <LIB>

    <LIB> is required — the Virtuoso library where the cell "flower"
    will be created.  Example::

        python flower.py testlib

    Running this script from VSCode without passing <LIB> will NOT work:
    the script will exit with a clear error, and Virtuoso will show nothing.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)

Customize the LAYER constants below to match your PDK metal stack.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_create_polygon as polygon,
    layout_create_path as path,
    layout_create_label as label,
    layout_fit_view as fit_view,
)

CELL = "flower"

N_PETALS = 8
PETAL_A = 3.5    # semi-major axis (petal length), um
PETAL_B = 1.2    # semi-minor axis (petal width), um
PETAL_D = 3.2    # petal center distance from origin, um
CENTER_R = 1.8   # center circle radius, um

# ----------------------------------------------------------------------
# Customize to match your PDK metal stack
# ----------------------------------------------------------------------
# Alternate two layers for petals so adjacent ones contrast in color.
# All layers listed here must be defined in your PDK techfile.
PETAL_LAYERS = [("M3", "drawing"), ("M4", "drawing")]
CENTER_LAYER = ("M5", "drawing")
STEM_LAYER   = ("M1", "drawing")
LEAF_LAYER   = ("M2", "drawing")
LABEL_LAYER  = ("M1", "pin")

# Available font names: "roman", "default", "times", "courier",
# "helvetica", "symbol", etc.  "roman" is the safest cross-PDK choice.
FONT = "roman"
# ----------------------------------------------------------------------


def _die(message: str) -> None:
    print(f"[ERROR] {message}", file=sys.stderr)
    raise SystemExit(1)


def ellipse_pts(
    cx: float, cy: float, a: float, b: float, angle: float, n: int = 28
) -> list[tuple[float, float]]:
    """Polygon approximation of an ellipse centred at (cx,cy), rotated by angle."""
    pts = []
    for i in range(n):
        phi = 2 * math.pi * i / n
        x = cx + a * math.cos(phi) * math.cos(angle) - b * math.sin(phi) * math.sin(angle)
        y = cy + a * math.cos(phi) * math.sin(angle) + b * math.sin(phi) * math.cos(angle)
        pts.append((round(x, 3), round(y, 3)))
    return pts


def main() -> int:
    # ------------------------------------------------------------------
    # Argument check — this script MUST be run with a library argument.
    # Clicking "Run" in VSCode without passing <LIB> will silently do
    # nothing in Virtuoso, so we abort with a clear message instead.
    # ------------------------------------------------------------------
    if len(sys.argv) < 2:
        print("=" * 60, file=sys.stderr)
        print(" ERROR: missing required argument <LIB>", file=sys.stderr)
        print()
        print(
            f" Usage: python {Path(__file__).name} <LIB>\n"
            " Example: python flower.py lifangshi\n",
            file=sys.stderr,
        )
        print(
            " NOTE: Running this script from VSCode (Ctrl+F5 / F5) will NOT\n"
            "       work — VSCode does not pass command-line arguments by default.\n"
            "       Either run from a terminal, configure a launch.json, or\n"
            "       edit the PDK values in this file directly.\n",
            file=sys.stderr,
        )
        print(
            " If Virtuoso shows a blank cell after running, check that:\n"
            "   1. <LIB> is a library that exists in your Virtuoso setup\n"
            "   2. All *LAYER constants match your PDK techfile layers\n",
            file=sys.stderr,
        )
        print("=" * 60, file=sys.stderr)
        return 1

    lib = sys.argv[1]

    client = VirtuosoClient.from_env()
    print(f"[Flower] Creating '{CELL}' in '{lib}' ...")
    print(f"  Layer (petals) : {[p[0] for p in PETAL_LAYERS]}")
    print(f"  Layer (stem)   : {STEM_LAYER[0]}")
    print(f"  Layer (label)  : {LABEL_LAYER[0]}")
    print(f"  Font           : {FONT}")
    print()

    with client.layout.edit(lib, CELL, mode="w") as layout:

        # -- Petals ----------------------------------------------------------------
        for i in range(N_PETALS):
            angle = math.pi * 2 * i / N_PETALS
            cx = PETAL_D * math.cos(angle)
            cy = PETAL_D * math.sin(angle)
            pts = ellipse_pts(cx, cy, PETAL_A, PETAL_B, angle)
            layer, purpose = PETAL_LAYERS[i % 2]
            layout.add(polygon(layer, purpose, pts))

        # -- Center circle ---------------------------------------------------------
        center_pts = ellipse_pts(0.0, 0.0, CENTER_R, CENTER_R, 0.0, n=32)
        layout.add(polygon(*CENTER_LAYER, center_pts))

        # -- Stem ------------------------------------------------------------------
        layout.add(path(*STEM_LAYER, [(0.0, -4.8), (0.0, -14.5)], width=0.6))

        # -- Leaves (one left, one right, staggered vertically) --------------------
        # Left leaf tilted upper-left
        leaf_l = ellipse_pts(-2.2, -8.5, 2.6, 0.85, math.radians(135), n=24)
        layout.add(polygon(*LEAF_LAYER, leaf_l))
        # Right leaf tilted lower-right
        leaf_r = ellipse_pts(2.2, -11.5, 2.6, 0.85, math.radians(45), n=24)
        layout.add(polygon(*LEAF_LAYER, leaf_r))

        # -- Label -----------------------------------------------------------------
        layout.add(label(*LABEL_LAYER, 0.0, -16.2, "FLOWER", "centerLeft", "R0", FONT, 0.6))

        layout.add(fit_view())

    client.open_window(lib, CELL, view="layout")
    print("[Done] Flower layout created — check the Virtuoso window.")
    print("       If the cell is blank, verify all *LAYER constants above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

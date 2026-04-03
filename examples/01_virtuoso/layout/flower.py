#!/usr/bin/env python3
"""Draw a flower in Virtuoso layout using polygons."""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from virtuoso_bridge import VirtuosoClient

LIB = "PLAYGROUND_LLM"
CELL = "flower"

N_PETALS = 8
PETAL_A = 3.5    # semi-major axis (petal length), μm
PETAL_B = 1.2    # semi-minor axis (petal width), μm
PETAL_D = 3.2    # petal center distance from origin, μm
CENTER_R = 1.8   # center circle radius, μm

# Alternate two layers for petals so adjacent ones contrast in color
PETAL_LAYERS = [("M3", "drawing"), ("M4", "drawing")]
CENTER_LAYER = ("M5", "drawing")
STEM_LAYER   = ("M1", "drawing")
LEAF_LAYER   = ("M2", "drawing")
LABEL_LAYER  = ("M1", "pin")


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
    client = VirtuosoClient.from_env()
    print(f"[Flower] Creating '{CELL}' in '{LIB}' ...")

    with client.layout.edit(LIB, CELL, mode="w") as layout:

        # ── Petals ────────────────────────────────────────────────────────────
        for i in range(N_PETALS):
            angle = math.pi * 2 * i / N_PETALS
            cx = PETAL_D * math.cos(angle)
            cy = PETAL_D * math.sin(angle)
            pts = ellipse_pts(cx, cy, PETAL_A, PETAL_B, angle)
            layer, purpose = PETAL_LAYERS[i % 2]
            layout.add_polygon(layer, purpose, pts)

        # ── Center circle ─────────────────────────────────────────────────────
        center_pts = ellipse_pts(0.0, 0.0, CENTER_R, CENTER_R, 0.0, n=32)
        layout.add_polygon(*CENTER_LAYER, center_pts)

        # ── Stem ──────────────────────────────────────────────────────────────
        layout.add_path(*STEM_LAYER, [(0.0, -4.8), (0.0, -14.5)], width=0.6)

        # ── Leaves (one left, one right, staggered vertically) ────────────────
        # Left leaf tilted upper-left
        leaf_l = ellipse_pts(-2.2, -8.5, 2.6, 0.85, math.radians(135), n=24)
        layout.add_polygon(*LEAF_LAYER, leaf_l)
        # Right leaf tilted lower-right
        leaf_r = ellipse_pts(2.2, -11.5, 2.6, 0.85, math.radians(45), n=24)
        layout.add_polygon(*LEAF_LAYER, leaf_r)

        # ── Label ─────────────────────────────────────────────────────────────
        layout.add_label(*LABEL_LAYER, xy=(0.0, -16.2), text="FLOWER", height=0.6)

        layout.fit_view()

    client.open_window(LIB, CELL, view="layout")
    print("[Done] Flower layout created and opened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

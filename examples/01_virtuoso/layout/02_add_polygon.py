#!/usr/bin/env python3
"""Add a polygon to the current layout view.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso (reads it via get_current_design)

Customize LAYER and PURPOSE below to match your PDK techfile.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_create_polygon as polygon,
)

LAYER = "edgeLayer"
PURPOSE = "drawing"
POINTS = [
    (0.5, 1.8),
    (2.0, 1.8),
    (2.5, 2.2),
    (1.2, 3.0),
    (0.5, 2.4),
]


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, design = timed_call(client.get_current_design)
    print(f"[get_current_design] [{format_elapsed(elapsed)}]")
    lib, cell, _ = design
    if not lib or not cell:
        print("Open a layout in Virtuoso first.")
        return 1

    print(f"Target Library  : {lib}")
    print(f"Target Cell     : {cell}")
    print(f"Layer/Purpose   : {LAYER}/{PURPOSE}")

    def add_polygon() -> None:
        with client.layout.edit(lib, cell, mode="a") as layout:
            layout.add(polygon(LAYER, PURPOSE, POINTS))

    edit_elapsed, _ = timed_call(add_polygon)
    print(f"[edit_layout] [{format_elapsed(edit_elapsed)}]")
    print("[Done] Polygon added to active layout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

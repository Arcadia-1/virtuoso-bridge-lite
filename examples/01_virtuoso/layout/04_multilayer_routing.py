#!/usr/bin/env python3
"""Add the same route across layout layers M2 through M7.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso

Customize LAYERS below to match the metal stack in your PDK techfile.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_create_path as path,
)

# ----------------------------------------------------------------------
# Customize to match your PDK metal stack
# ----------------------------------------------------------------------
# List of metal layers available in your PDK (in routing order, bottom→top)
LAYERS = ["M2", "M3", "M4", "M5", "M6", "M7"]
# ----------------------------------------------------------------------

# Routing parameters
PATH_WIDTH = 0.1   # um
X_START    = 0.0
X_END      = 5.0
Y          = 3.0


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

    def add_routing() -> None:
        with client.layout.edit(lib, cell, mode="a") as layout:
            for layer in LAYERS:
                layout.add(path(layer, "drawing", [(X_START, Y), (X_END, Y)], width=PATH_WIDTH))

    edit_elapsed, _ = timed_call(add_routing)
    print(f"[edit_layout] [{format_elapsed(edit_elapsed)}]")

    print("[Done] Multi-layer routing added (M2-M7)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

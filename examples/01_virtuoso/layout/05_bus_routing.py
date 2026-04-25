#!/usr/bin/env python3
"""Add an 8-bit labeled bus route to the current layout view.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso

Customize LAYERS, LABEL_LAYER and FONT below to match your PDK techfile.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_create_path as path,
    layout_create_label as label,
)

# ----------------------------------------------------------------------
# Customize to match your PDK metal stack
# ----------------------------------------------------------------------
# Metal layer(s) for bus wires — must be defined in your PDK techfile
LAYERS    = ["M4"]
# ----------------------------------------------------------------------

BUS_WIDTH = 8

# Routing parameters
PATH_WIDTH = 0.05  # um, wire width
BUS_PITCH  = 0.1   # um, spacing between bit wires
X_START    = 0.0
X_END      = 5.0
Y_BASE     = 2.0   # Y of bit 0 (CODE<0>); higher bits increment upward

# ----------------------------------------------------------------------
# Customize to match your PDK techfile
# ----------------------------------------------------------------------
# Layer/purpose for bus labels — must be defined in your PDK techfile
LABEL_LAYER  = "M4"
LABEL_HEIGHT = 0.1  # um

# Available font names: "roman", "default", "times", "courier",
# "helvetica", "symbol", etc.  "roman" is the safest cross-PDK choice.
FONT = "roman"
# ----------------------------------------------------------------------


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, design = timed_call(client.get_current_design)
    print(f"[get_current_design] [{format_elapsed(elapsed)}]")
    lib, cell, _ = design
    if not lib or not cell:
        print("No layout window open in Virtuoso.")
        return 1

    print(f"Target Library  : {lib}")
    print(f"Target Cell     : {cell}")

    def add_bus() -> None:
        with client.layout.edit(lib, cell, mode="a") as layout:
            for bit in range(BUS_WIDTH):
                y = Y_BASE + bit * BUS_PITCH

                # Multi-layer path on every layer at the same coordinate
                for layer in LAYERS:
                    layout.add(path(layer, "drawing", [(X_START, y), (X_END, y)], width=PATH_WIDTH))

                # Label at the left end
                layout.add(label(
                    LABEL_LAYER, "pin",
                    X_START, y,
                    f"CODE<{bit}>",
                    "centerLeft", "R0", FONT,
                    LABEL_HEIGHT,
                ))

    edit_elapsed, _ = timed_call(add_bus)
    print(f"[edit_layout] [{format_elapsed(edit_elapsed)}]")

    print("[Done] 8-bit bus routing CODE<7:0> added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

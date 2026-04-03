#!/usr/bin/env python3
"""Add an 8-bit labeled bus route to the current layout view."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

LAYERS    = ["M4"]
BUS_WIDTH = 8

# Routing parameters
PATH_WIDTH = 0.05  # um, wire width
BUS_PITCH  = 0.1   # um, spacing between bit wires
X_START    = 0.0
X_END      = 5.0
Y_BASE     = 2.0   # Y of bit 0 (CODE<0>); higher bits increment upward

LABEL_LAYER  = "M4"
LABEL_HEIGHT = 0.1  # um


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, design = timed_call(client.get_current_design)
    print(f"[get_current_design] [{format_elapsed(elapsed)}]")
    lib, cell, _ = design
    if not lib:
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
                    layout.add_path(layer, "drawing", [(X_START, y), (X_END, y)], width=PATH_WIDTH)

                # Label at the left end
                layout.add_label(
                    LABEL_LAYER, "pin",
                    (X_START, y),
                    text=f"CODE<{bit}>",
                    justification="centerLeft",
                    height=LABEL_HEIGHT,
                )

    edit_elapsed, _ = timed_call(add_bus)
    print(f"[edit_layout] [{format_elapsed(edit_elapsed)}]")

    print("[Done] 8-bit bus routing CODE<7:0> added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Add the same route across layout layers M2 through M7."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

LAYERS = ["M2", "M3", "M4", "M5", "M6", "M7"]

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
    if not lib:
        print("Open a layout in Virtuoso first.")
        return 1

    print(f"Target Library  : {lib}")
    print(f"Target Cell     : {cell}")

    def add_routing() -> None:
        with client.layout.edit(lib, cell, mode="a") as layout:
            for layer in LAYERS:
                layout.add_path(layer, "drawing", [(X_START, Y), (X_END, Y)], width=PATH_WIDTH)

    edit_elapsed, _ = timed_call(add_routing)
    print(f"[edit_layout] [{format_elapsed(edit_elapsed)}]")

    print("[Done] Multi-layer routing added (M2-M7)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

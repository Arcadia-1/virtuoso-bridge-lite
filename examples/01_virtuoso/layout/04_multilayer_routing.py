#!/usr/bin/env python3
"""Add the same route across layout layers M2 through M7.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso

Customize LAYERS below, or pass --layers / --layer-map, to match the metal
stack in your PDK techfile; layers missing from it are reported before anything
is drawn.

Usage::

    python 04_multilayer_routing.py [--layers M2,M3] [--layer-map M2=METAL2,M3=METAL3]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _pdk import add_layer_map_argument, parse_layer_map, rename_layer, require_layers
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--layers", default=",".join(LAYERS),
                        help="metal layers to route on, bottom to top")
    add_layer_map_argument(parser)
    args = parser.parse_args()
    mapping = parse_layer_map(args.layer_map)
    layers = [rename_layer(layer, mapping) for layer in args.layers.split(",") if layer]

    client = VirtuosoClient.from_env()

    elapsed, design = timed_call(client.get_current_design)
    print(f"[get_current_design] [{format_elapsed(elapsed)}]")
    lib, cell, view = design
    if not lib or not cell or view != "layout":
        print("Open a layout cellview in Virtuoso first.")
        return 1
    require_layers(client, lib, [(layer, "drawing") for layer in layers])

    print(f"Target Library  : {lib}")
    print(f"Target Cell     : {cell}")

    def add_routing() -> None:
        with client.layout.modify(lib, cell) as layout:
            for layer in layers:
                layout.add(path(layer, "drawing", [(X_START, Y), (X_END, Y)], width=PATH_WIDTH))

    edit_elapsed, _ = timed_call(add_routing)
    print(f"[edit_layout] [{format_elapsed(edit_elapsed)}]")

    print(f"[Done] Multi-layer routing added ({', '.join(layers)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create a demo layout with shapes and instances.

Usage::

    python 01_create_layout.py <LIB>

    <LIB> is required — the target Virtuoso library where the layout cell
    will be created.  Example::

        python 01_create_layout.py testlib

    Running this script from VSCode without passing <LIB> will NOT work:
    the script will exit with a clear error, and Virtuoso will show nothing.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - PDK library and layers below must match your PDK definition

Customize the PDK_*, LAYER_* and FONT constants below, or pass --pdk-lib,
--pdk-cells and --layer-map, to match your environment; missing layers or
masters are reported before anything is created.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _pdk import add_layer_map_argument, parse_layer_map, rename_layer, require_cell, require_layers
from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_create_param_inst as inst,
    layout_create_rect as rect,
    layout_create_path as path,
    layout_create_label as label,
)

# ----------------------------------------------------------------------
# Customize these to match your PDK and environment
# ----------------------------------------------------------------------
# PDK library that contains your device masters (e.g. nch, pch, cap, res...)
# Verify this library exists in your Virtuoso library manager before running.
PDK_LIB = "tsmcN28"
PDK_CELLS = ("nch_ulvt_mac", "pch_ulvt_mac", "cfmom_2t")

# Layer/purpose names for shapes — must be defined in your PDK techfile.
# Common examples: M1/drawing, Metal1/drawing, poly/drawing, etc.
# Check layer names in Virtuoso: Setup → Layers → Layer Table.
LAYER_RECT  = "M1"      # layer for rectangles
LAYER_PATH  = "M2"      # layer for paths
LAYER_LABEL = "M1"      # layer for text labels
LAYER_PIN   = "M1"      # layer for pin labels

# Available font names in Virtuoso: "default", "roman", "times", "courier",
# "helvetica", "symbol", etc.  "roman" is the safest cross-PDK choice.
FONT = "roman"
# ----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a demo layout with shapes and PDK instances.",
        epilog="Example for tsmc18: --pdk-lib tsmc18 --pdk-cells nmos2v,pmos2v "
               "--layer-map M1=METAL1,M2=METAL2",
    )
    parser.add_argument("lib", help="existing library where the layout cell is created")
    parser.add_argument("--pdk-lib", default=PDK_LIB, help="library of the device masters")
    parser.add_argument("--pdk-cells", default=",".join(PDK_CELLS),
                        help="up to three layout masters to place, comma separated")
    parser.add_argument("--skip-instances", action="store_true", help="draw shapes only")
    add_layer_map_argument(parser)
    args = parser.parse_args()
    mapping = parse_layer_map(args.layer_map)
    layer_rect, layer_path, layer_label = (
        rename_layer(layer, mapping) for layer in (LAYER_RECT, LAYER_PATH, LAYER_LABEL)
    )
    pdk_cells = [] if args.skip_instances else [c for c in args.pdk_cells.split(",") if c][:3]

    lib_name = args.lib
    cell_name = f"layout_demo_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    client = VirtuosoClient.from_env()
    require_layers(client, lib_name, [(layer_rect, "drawing"), (layer_path, "drawing"), (layer_label, "pin")])
    for pdk_cell in pdk_cells:
        require_cell(client, args.pdk_lib, pdk_cell)

    # Quick sanity check: print what we are about to do so the user can
    # verify the values before the layout is created.
    print(f"Target Library  : {lib_name}")
    print(f"Target Cell    : {cell_name}")
    print(f"PDK masters    : {', '.join(f'{args.pdk_lib}/{c}' for c in pdk_cells) or '(none)'}")
    print(f"Layer (rect)   : {layer_rect}/drawing")
    print(f"Layer (path)   : {layer_path}/drawing")
    print(f"Layer (label)  : {layer_label}/pin")
    print(f"Font           : {FONT}")
    print()

    def build_layout() -> None:
        with client.layout.create(lib_name, cell_name) as layout:
            # --- instances (device masters from --pdk-lib) ---
            for index, pdk_cell in enumerate(pdk_cells):
                layout.add(inst(args.pdk_lib, pdk_cell, "layout", f"I{index}", 2.0 * index, 0.0, "R0"))

            # --- shapes (adjust LAYER_* constants above) ---
            layout.add(rect(layer_rect,  "drawing", 1.0, 0.0, 2.0, 0.5))
            layout.add(rect(layer_rect,  "drawing", 1.5, 1.0, 2.5, 1.5))
            layout.add(path(layer_path, "drawing", [(1.0, 0.25), (3.0, 0.25)], width=0.1))
            layout.add(label(layer_label, "pin",  1.1, 0.5, "IN", "centerLeft", "R0", FONT, 0.1))

    elapsed, _ = timed_call(build_layout)
    print(f"[edit_layout]  [{format_elapsed(elapsed)}]")

    open_elapsed, _ = timed_call(lambda: client.open_window(lib_name, cell_name, view="layout"))
    print(f"[open_window] [{format_elapsed(open_elapsed)}]")
    print("[Done] Layout created — check the Virtuoso window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

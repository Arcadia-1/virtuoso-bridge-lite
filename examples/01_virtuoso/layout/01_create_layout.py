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

Customize the PDK_*, LAYER_* and FONT constants below to match your
environment before running.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

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
            " Example: python 01_create_layout.py lifangshi\n",
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
            "   2. PDK_LIB points to a library with device masters\n"
            "   3. LAYER_* names match your PDK techfile layers\n",
            file=sys.stderr,
        )
        print("=" * 60, file=sys.stderr)
        raise SystemExit(1)

    lib_name = sys.argv[1]
    cell_name = f"layout_demo_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    client = VirtuosoClient.from_env()

    # Quick sanity check: print what we are about to do so the user can
    # verify the values before the layout is created.
    print(f"Target Library  : {lib_name}")
    print(f"Target Cell    : {cell_name}")
    print(f"PDK Library    : {PDK_LIB}")
    print(f"Layer (rect)   : {LAYER_RECT}/drawing")
    print(f"Layer (path)   : {LAYER_PATH}/drawing")
    print(f"Layer (label)  : {LAYER_LABEL}/pin")
    print(f"Font           : {FONT}")
    print()

    def build_layout() -> None:
        with client.layout.edit(lib_name, cell_name) as layout:
            # --- instances (device masters from PDK_LIB) ---
            layout.add(inst(PDK_LIB, "nch_ulvt_mac", "layout", "M0", 0.0, 0.0, "R0"))
            layout.add(inst(PDK_LIB, "pch_ulvt_mac", "layout", "M1", 2.0, 0.0, "R0"))
            layout.add(inst(PDK_LIB, "cfmom_2t",     "layout", "C0", 4.0, 0.0, "R0"))

            # --- shapes (adjust LAYER_* constants above) ---
            layout.add(rect(LAYER_RECT,  "drawing", 1.0, 0.0, 2.0, 0.5))
            layout.add(rect(LAYER_RECT,  "drawing", 1.5, 1.0, 2.5, 1.5))
            layout.add(path(LAYER_PATH, "drawing", [(1.0, 0.25), (3.0, 0.25)], width=0.1))
            layout.add(label(LAYER_LABEL, "pin",  1.1, 0.5, "IN", "centerLeft", "R0", FONT, 0.1))

    elapsed, _ = timed_call(build_layout)
    print(f"[edit_layout]  [{format_elapsed(elapsed)}]")

    open_elapsed, _ = timed_call(lambda: client.open_window(lib_name, cell_name, view="layout"))
    print(f"[open_window] [{format_elapsed(open_elapsed)}]")
    print("[Done] Layout created — check the Virtuoso window.")
    print("       If the cell is blank, verify PDK_LIB and LAYER_* values above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

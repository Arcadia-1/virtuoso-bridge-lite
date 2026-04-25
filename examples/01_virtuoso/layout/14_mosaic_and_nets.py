#!/usr/bin/env python3
"""Create a mosaic array and highlight a net in the current layout.

Demonstrates:
  1. layout_create_simple_mosaic — array a cell as a mosaic instance
  2. layout_highlight_net         — highlight shapes belonging to a named net
  3. layout_set_active_lpp        — set the active layer-purpose pair

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso

Customize MOSAIC_* and NET_NAME below to match your design.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_create_simple_mosaic,
    layout_fit_view,
    layout_highlight_net,
    layout_set_active_lpp,
)

# ----------------------------------------------------------------------
# Customize to match your design
# ----------------------------------------------------------------------
# The cell to array — must exist in Virtuoso as a layout cellview.
# A good candidate is the cell created by 01_create_layout.py.
MOSAIC_LIB  = "tsmcN28"
MOSAIC_CELL = "nch_ulvt_mac"

# Mosaic parameters
ROWS       = 2
COLS       = 3
ROW_PITCH  = 2.0  # um, vertical spacing between rows
COL_PITCH  = 2.0  # um, horizontal spacing between columns
ORIGIN     = (0.0, 0.0)
ORIENT     = "R0"

# Net name to highlight — must correspond to a named net in the layout.
# For cells created by 01_create_layout.py, "IN" is the pin label.
NET_NAME = "IN"

# Layer to set as active after mosaic creation
ACTIVE_LAYER   = "M1"
ACTIVE_PURPOSE = "drawing"
# ----------------------------------------------------------------------


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, design = timed_call(client.get_current_design)
    print(f"[get_current_design] [{format_elapsed(elapsed)}]")
    lib, cell, view = design
    if not lib or not cell or view != "layout":
        print("Open a layout cellview in Virtuoso first.")
        return 1

    print(f"Target Library  : {lib}")
    print(f"Target Cell     : {cell}")
    print(f"Mosaic Master   : {MOSAIC_LIB}/{MOSAIC_CELL}")
    print(f"Array           : {ROWS}x{COLS}, pitch ({ROW_PITCH}, {COL_PITCH}) um")
    print(f"Net to highlight: {NET_NAME}")
    print()

    # Step 1: create mosaic
    mosaic_elapsed, mosaic_result = timed_call(
        lambda: client.execute_skill(
            layout_create_simple_mosaic(
                MOSAIC_LIB,
                MOSAIC_CELL,
                origin=ORIGIN,
                orientation=ORIENT,
                rows=ROWS,
                cols=COLS,
                row_pitch=ROW_PITCH,
                col_pitch=COL_PITCH,
            ),
            timeout=30,
        )
    )
    print(f"[layout_create_simple_mosaic] [{format_elapsed(mosaic_elapsed)}]")
    print(decode_skill(mosaic_result.output or ""))
    print()

    # Step 2: highlight net
    net_elapsed, net_result = timed_call(
        lambda: client.execute_skill(layout_highlight_net(NET_NAME), timeout=15)
    )
    print(f"[layout_highlight_net] [{format_elapsed(net_elapsed)}]")
    net_out = decode_skill(net_result.output or "")
    print(net_out or "  (no output)")
    if net_out.startswith("ERROR"):
        print(f"  → Net '{NET_NAME}' not found — this is normal if the cell has no such net.")
    print()

    # Step 3: set active layer
    lpp_elapsed, lpp_result = timed_call(
        lambda: client.execute_skill(
            layout_set_active_lpp(ACTIVE_LAYER, ACTIVE_PURPOSE), timeout=10
        )
    )
    print(f"[layout_set_active_lpp] [{format_elapsed(lpp_elapsed)}]")
    print(decode_skill(lpp_result.output or ""))

    # Fit to see everything
    fit_elapsed, _ = timed_call(
        lambda: client.execute_skill(layout_fit_view(), timeout=10)
    )
    print(f"[layout_fit_view] [{format_elapsed(fit_elapsed)}]")
    print("[Done] Mosaic created, net highlighted, active layer set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

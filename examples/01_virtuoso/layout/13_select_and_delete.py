#!/usr/bin/env python3
"""Select shapes in a bounding box and delete them from the current layout.

Demonstrates:
  1. layout_select_box  — select all figures within a rectangle region
  2. layout_delete_selected — delete the current selection

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso

Customize BOX below to match the region you want to target.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_delete_selected,
    layout_fit_view,
    layout_select_box,
)

# ----------------------------------------------------------------------
# Customize: bounding box of the region to select and delete
# ----------------------------------------------------------------------
# (x0, y0, x1, y1) — lower-left and upper-right corners in microns
BOX = (0.5, 0.0, 2.5, 2.0)
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
    print(f"Select box      : {BOX}")
    print()

    # Step 1: select
    sel_elapsed, sel_result = timed_call(
        lambda: client.execute_skill(layout_select_box(BOX), timeout=15)
    )
    print(f"[layout_select_box] [{format_elapsed(sel_elapsed)}]")
    sel_out = decode_skill(sel_result.output or "")
    print(sel_out or "  (no output)")
    if sel_out.startswith("ERROR"):
        print("[Abort] Selection failed.")
        return 1

    # Step 2: delete
    del_elapsed, del_result = timed_call(
        lambda: client.execute_skill(layout_delete_selected(), timeout=15)
    )
    print(f"[layout_delete_selected] [{format_elapsed(del_elapsed)}]")
    print(decode_skill(del_result.output or ""))

    # Fit to confirm
    fit_elapsed, _ = timed_call(
        lambda: client.execute_skill(layout_fit_view(), timeout=10)
    )
    print(f"[layout_fit_view] [{format_elapsed(fit_elapsed)}]")
    print("[Done] Shapes in the selected region have been deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

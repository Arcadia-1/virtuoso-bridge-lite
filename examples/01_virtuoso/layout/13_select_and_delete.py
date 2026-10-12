#!/usr/bin/env python3
"""Select shapes in a bounding box of one explicit layout and delete them.

Usage::

    python 13_select_and_delete.py <LIB> <CELL> [--yes]

Opens the cell's layout window, checks it is the current one, then selects
the figures inside BOX and deletes them (GUI operations on that window).
Customize BOX below to match the region you want to target.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from _target import parse_target
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
    args = parse_target("Delete the figures inside BOX in one layout window.", destructive=True)
    client = VirtuosoClient.from_env()

    client.open_window(args.lib, args.cell, view="layout")
    if client.get_current_design() != (args.lib, args.cell, "layout"):
        print(f"Could not make {args.lib}/{args.cell} the current layout window.")
        return 1

    print(f"Target          : {args.lib}/{args.cell}")
    print(f"Select box      : {BOX}")

    sel_elapsed, sel_result = timed_call(
        lambda: client.execute_skill(layout_select_box(BOX), timeout=15)
    )
    print(f"[layout_select_box] [{format_elapsed(sel_elapsed)}]")
    sel_out = decode_skill(sel_result.output or "")
    print(sel_out or "  (no output)")
    if sel_result.status.value != "success" or sel_out.startswith("ERROR"):
        print("[Abort] Selection failed.")
        return 1
    if not args.yes:
        print("Would delete the selected figures; pass --yes to do it.")
        return 0

    del_elapsed, del_result = timed_call(
        lambda: client.execute_skill(layout_delete_selected(), timeout=15)
    )
    print(f"[layout_delete_selected] [{format_elapsed(del_elapsed)}]")
    print(decode_skill(del_result.output or ""))
    timed_call(lambda: client.execute_skill(layout_fit_view(), timeout=10))
    return 0 if del_result.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

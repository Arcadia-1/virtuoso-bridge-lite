#!/usr/bin/env python3
"""Add vias with both the by-name and raw-viaDef APIs.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso

Customize VIA_NAME below to match a via definition in your PDK techfile
(e.g. M1_M2, M2_M3, etc.).  Check via names via Virtuoso UI:
  Execute → Create → Via...
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_create_via_by_name as via_by_name,
    layout_create_via as via,
    layout_via_def_expr_from_name as via_def_from_name,
)

# ----------------------------------------------------------------------
# Customize to match your PDK
# ----------------------------------------------------------------------
# Via definition name — must exist in your PDK techfile
VIA_NAME = "M2_M1"
# ----------------------------------------------------------------------

BY_NAME_VIA_X = 1.5
BY_NAME_VIA_Y = 0.25
RAW_VIA_X = 2.0
RAW_VIA_Y = 0.5
VIA_ORIENTATION = "R0"


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
    print(f"Via Name        : {VIA_NAME}")

    def add_vias() -> None:
        with client.layout.edit(lib, cell, mode="a") as layout:
            layout.add(via_by_name(
                VIA_NAME,
                BY_NAME_VIA_X,
                BY_NAME_VIA_Y,
                orientation=VIA_ORIENTATION,
            ))
            layout.add(via(
                via_def_from_name(VIA_NAME),
                RAW_VIA_X,
                RAW_VIA_Y,
                VIA_ORIENTATION,
                "nil",
            ))

    edit_elapsed, _ = timed_call(add_vias)
    print(f"[edit_layout] [{format_elapsed(edit_elapsed)}]")
    print("[Done] Added via-by-name and raw via to active layout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

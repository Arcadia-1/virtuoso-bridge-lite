#!/usr/bin/env python3
"""Control layer visibility in the current layout window.

Demonstrates three layer-visibility APIs in sequence:
  1. hide_layers   — hide specific layer-purpose pairs
  2. show_layers   — restore visibility of hidden layers
  3. show_only_layers — hide everything, then show only requested layers

Each step pauses briefly so you can observe the change in Virtuoso.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso

Customize LAYERS below to match your PDK techfile.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_fit_view,
    layout_hide_layers,
    layout_show_layers,
    layout_show_only_layers,
)

# ----------------------------------------------------------------------
# Customize to match your PDK metal stack
# ----------------------------------------------------------------------
# Layer-purpose pairs to toggle. All must be defined in your PDK techfile.
LAYERS = [
    ("M3", "drawing"),
    ("M4", "drawing"),
]
# ----------------------------------------------------------------------


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, design = timed_call(client.get_current_design)
    print(f"[get_current_design] [{format_elapsed(elapsed)}]")
    lib, cell, view = design
    if not lib or not cell or view != "layout":
        print("Open a layout cellview in Virtuoso first.")
        return 1

    layer_names = ", ".join(f"{l}/{p}" for l, p in LAYERS)
    print(f"Target Library  : {lib}")
    print(f"Target Cell     : {cell}")
    print(f"Layers          : {layer_names}")
    print()

    # Step 1: hide
    hide_elapsed, hide_result = timed_call(
        lambda: client.execute_skill(layout_hide_layers(LAYERS), timeout=15)
    )
    print(f"[layout_hide_layers] [{format_elapsed(hide_elapsed)}]")
    print(decode_skill(hide_result.output or ""))
    print(f"  → Check Virtuoso: {layer_names} should now be invisible.\n")

    # Step 2: restore
    show_elapsed, show_result = timed_call(
        lambda: client.execute_skill(layout_show_layers(LAYERS), timeout=15)
    )
    print(f"[layout_show_layers] [{format_elapsed(show_elapsed)}]")
    print(decode_skill(show_result.output or ""))
    print(f"  → Check Virtuoso: {layer_names} should be visible again.\n")

    # Step 3: show only
    only_elapsed, only_result = timed_call(
        lambda: client.execute_skill(layout_show_only_layers(LAYERS), timeout=15)
    )
    print(f"[layout_show_only_layers] [{format_elapsed(only_elapsed)}]")
    print(decode_skill(only_result.output or ""))
    print(f"  → Check Virtuoso: only {layer_names} should be visible.\n")

    # Fit to see the result clearly
    fit_elapsed, _ = timed_call(
        lambda: client.execute_skill(layout_fit_view(), timeout=10)
    )
    print(f"[layout_fit_view] [{format_elapsed(fit_elapsed)}]")
    print("[Done] Layer visibility demo complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

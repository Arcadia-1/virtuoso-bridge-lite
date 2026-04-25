#!/usr/bin/env python3
"""Delete all shapes on a target layer and purpose from the current layout.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso

Customize DELETE_LAYER and DELETE_PURPOSE below to match your PDK techfile.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import (
    layout_delete_shapes_on_layer,
    layout_list_shapes,
)

# ----------------------------------------------------------------------
# Customize to match the layer/purpose you want to delete
# ----------------------------------------------------------------------
# Must be defined in your PDK techfile
DELETE_LAYER   = "M3"
DELETE_PURPOSE = "drawing"
# ----------------------------------------------------------------------


def main() -> int:
    client = VirtuosoClient.from_env()

    # Always list shapes first
    result = client.execute_skill(layout_list_shapes(), timeout=15)
    shapes = decode_skill(result.output or "")
    print("Shapes in open layout:")
    print(shapes or "  (none)")

    delete_elapsed, result = timed_call(
        lambda: client.execute_skill(
            layout_delete_shapes_on_layer(DELETE_LAYER, DELETE_PURPOSE), timeout=30
        )
    )
    print(f"[layout_delete_shapes_on_layer] [{format_elapsed(delete_elapsed)}]")
    print(decode_skill(result.output or ""))

    # Save after delete
    save_elapsed, save_result = timed_call(lambda: client.save_current_cellview(timeout=15))
    print(f"[save_current_cellview] [{format_elapsed(save_elapsed)}]")
    print(decode_skill(save_result.output or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

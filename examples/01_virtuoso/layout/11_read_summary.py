#!/usr/bin/env python3
"""Read a lightweight summary of shapes and instances from the current layout cell.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A layout cellview must be open in Virtuoso
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import layout_read_summary


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, design = timed_call(client.get_current_design)
    print(f"[get_current_design] [{format_elapsed(elapsed)}]")
    lib, cell, view = design
    if not lib or not cell or view != "layout":
        print("Open a layout cellview in Virtuoso first.")
        return 1

    read_elapsed, result = timed_call(
        lambda: client.execute_skill(layout_read_summary(lib, cell), timeout=30)
    )
    print(f"[layout_read_summary] [{format_elapsed(read_elapsed)}]")

    output = decode_skill(result.output or "")
    if output.startswith("ERROR"):
        print(output)
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

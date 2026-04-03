#!/usr/bin/env python3
"""Close the current layout cellview and delete the entire cell."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, design = timed_call(client.get_current_design)
    print(f"[get_current_design] [{format_elapsed(elapsed)}]")
    lib, cell, view = design
    if not lib or view != "layout":
        print("No active layout window.")
        return 1

    close_elapsed, close_result = timed_call(lambda: client.close_current_cellview(timeout=15))
    if close_result.ok:
        print(f"[close_current_cellview] [{format_elapsed(close_elapsed)}]")
    else:
        errors = close_result.errors or ["close failed"]
        print(f"[close_current_cellview] failed: {errors[0]}")

    delete_elapsed, result = timed_call(lambda: client.layout.delete_cell(lib, cell, timeout=30))
    print(f"[layout.delete_cell] [{format_elapsed(delete_elapsed)}]")
    print(decode_skill(result.output or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read full connectivity from a schematic.

Usage::

    python 02_read_connectivity.py MYLIB MYCELL
    python 02_read_connectivity.py              # uses the active design

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "read_connectivity.il"


def main() -> int:
    client = VirtuosoClient.from_env()

    if len(sys.argv) >= 3:
        lib, cell = sys.argv[1], sys.argv[2]
    else:
        lib, cell, _ = client.get_current_design()
        if not lib:
            print("Usage: python 02_read_connectivity.py LIB CELL")
            print("       or open a schematic in Virtuoso first.")
            return 1

    load_elapsed, load_result = timed_call(lambda: client.load_il(IL_FILE))
    meta = load_result.metadata
    print(f"[load_il] {'uploaded' if meta.get('uploaded') else 'cache hit'}  [{format_elapsed(load_elapsed)}]")

    exec_elapsed, result = timed_call(
        lambda: client.execute_skill(f'ReadSchematic("{lib}" "{cell}")', timeout=30)
    )
    print(f"[execute_skill] [{format_elapsed(exec_elapsed)}]")
    print()

    output = decode_skill(result.output or "")
    if not output or output.startswith("ERROR"):
        print(output or "No output returned.")
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

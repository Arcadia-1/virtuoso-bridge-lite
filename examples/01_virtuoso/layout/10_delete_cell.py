#!/usr/bin/env python3
"""Close the windows of one explicit layout cell and delete the cell.

Usage::

    python 10_delete_cell.py <LIB> <CELL> [--yes]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from _target import parse_target
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import layout_delete_cell


def main() -> int:
    args = parse_target("Close and delete one explicit cell.", destructive=True)
    client = VirtuosoClient.from_env()
    if not args.yes:
        print(f"Would delete {args.lib}/{args.cell} and all its views; pass --yes to do it.")
        return 0

    elapsed, result = timed_call(
        lambda: client.execute_skill(layout_delete_cell(args.lib, args.cell), timeout=30)
    )
    print(f"[layout_delete_cell] [{format_elapsed(elapsed)}]")
    print(decode_skill(result.output or "") or result.errors)
    return 0 if result.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

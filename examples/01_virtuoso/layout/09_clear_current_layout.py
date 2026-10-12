#!/usr/bin/env python3
"""Clear all visible figures of one explicit layout through its window.

Usage::

    python 09_clear_current_layout.py <LIB> <CELL> [--yes]

Opens the cell's layout window, checks it is the current one, then deletes
every visible figure in it (a GUI operation on the current window).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from _target import parse_target
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout import clear_current_layout


def main() -> int:
    args = parse_target("Delete all visible figures in one layout window.", destructive=True)
    client = VirtuosoClient.from_env()

    client.open_window(args.lib, args.cell, view="layout")
    if client.get_current_design() != (args.lib, args.cell, "layout"):
        print(f"Could not make {args.lib}/{args.cell} the current layout window.")
        return 1

    if not args.yes:
        print(f"Would delete every visible figure in {args.lib}/{args.cell}; pass --yes to do it.")
        return 0

    elapsed, result = timed_call(
        lambda: client.execute_skill(clear_current_layout(), timeout=30)
    )
    print(f"[clear_current_layout] [{format_elapsed(elapsed)}]")
    output = decode_skill(result.output or "")
    if output:
        print(output)
    for error in result.errors or []:
        print(f"[error] {error}")
    return 0 if result.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

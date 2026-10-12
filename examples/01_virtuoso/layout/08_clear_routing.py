#!/usr/bin/env python3
"""Clear routing shapes from one explicit layout while keeping instances.

Usage::

    python 08_clear_routing.py <LIB> <CELL> [--yes]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from _target import parse_target
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.layout.ops import layout_clear_routing


def main() -> int:
    args = parse_target("Delete all shapes (instances kept) from one layout.", destructive=True)
    client = VirtuosoClient.from_env()
    target = (f'dbOpenCellViewByType("{escape_skill_string(args.lib)}" '
              f'"{escape_skill_string(args.cell)}" "layout" "maskLayout" "a")')
    if not args.yes:
        count = client.execute_skill(f"length({target}~>shapes)").output
        print(f"Would delete {count} shape(s) from {args.lib}/{args.cell}; pass --yes to do it.")
        return 0

    elapsed, result = timed_call(
        lambda: client.execute_skill(layout_clear_routing(cv_expr=target), timeout=30)
    )
    print(f"[layout_clear_routing] [{format_elapsed(elapsed)}]")
    print(decode_skill(result.output or "") or result.errors)
    return 0 if result.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

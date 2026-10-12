#!/usr/bin/env python3
"""Close and delete one explicit cell.

Usage::

    python 07_delete_cell.py <LIB> <CELL> [--yes]

Without --yes it only reports what it would delete.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from _target import parse_target
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string


def main() -> int:
    args = parse_target("Close and delete one explicit cell.", destructive=True)
    client = VirtuosoClient.from_env()
    lib, cell = escape_skill_string(args.lib), escape_skill_string(args.cell)

    exists = client.execute_skill(f'ddGetObj("{lib}" "{cell}")').output not in (None, "", "nil")
    if not exists:
        print(f"Cell not found: {args.lib}/{args.cell}")
        return 1
    if not args.yes:
        print(f"Would delete {args.lib}/{args.cell} and all its views; pass --yes to do it.")
        return 0

    elapsed, r = timed_call(lambda: client.execute_skill(f"""
let((cv)
  foreach(win hiGetWindowList()
    cv = car(errset(geGetWindowCellView(win)))
    when(cv && cv~>libName == "{lib}" && cv~>cellName == "{cell}"
      when(cv~>modifiedButNotSaved dbSave(cv))
      hiCloseWindow(win)))
  foreach(open dbGetOpenCellViews()
    when(open~>libName == "{lib}" && open~>cellName == "{cell}" dbPurge(open)))
  if(ddDeleteObj(ddGetObj("{lib}" "{cell}"))
    then sprintf(nil "deleted: %s/%s" "{lib}" "{cell}")
    else error("delete failed: %s/%s" "{lib}" "{cell}")))
""", timeout=30))
    print(f"{r.output or r.errors}  [{format_elapsed(elapsed)}]")
    return 0 if r.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

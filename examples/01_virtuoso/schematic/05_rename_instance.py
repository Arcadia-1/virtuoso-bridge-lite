#!/usr/bin/env python3
"""Rename instances in the currently open schematic, check and save.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A schematic open in Virtuoso (e.g. created by 01a_create_rc_stepwise.py)

Customize RENAMES below to specify which instances to rename and their new names.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

# ----------------------------------------------------------------------
# Customize: list of (old_name, new_name) pairs to rename
# ----------------------------------------------------------------------
RENAMES = [("I0", "IAAA_RENAMED"), ("R0", "RBBB_RENAMED")]
# ----------------------------------------------------------------------


def main() -> int:
    client = VirtuosoClient.from_env()

    for old, new in RENAMES:
        r = client.execute_skill(f'''
let((cv inst)
  cv = geGetEditCellView()
  inst = car(setof(x cv~>instances x~>name == "{old}"))
  when(inst inst~>name = "{new}" sprintf(nil "renamed: {old} -> {new}")))
''')
        print(r.output or f"  {old}: not found")

    # schCheck + save
    elapsed, r = timed_call(lambda: client.execute_skill(
        'let((cv) cv = geGetEditCellView() schCheck(cv) dbSave(cv) "saved")'))
    print(f"[save] [{format_elapsed(elapsed)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

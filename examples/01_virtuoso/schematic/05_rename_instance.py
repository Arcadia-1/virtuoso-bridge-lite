#!/usr/bin/env python3
"""Rename instances in one explicit schematic, check and save.

Usage::

    python 05_rename_instance.py <LIB> <CELL>

Customize RENAMES below to choose which instances to rename.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from _target import parse_target
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string

# ----------------------------------------------------------------------
# Customize: list of (old_name, new_name) pairs to rename
# ----------------------------------------------------------------------
RENAMES = [("I0", "IAAA_RENAMED"), ("R0", "RBBB_RENAMED")]
# ----------------------------------------------------------------------


def main() -> int:
    args = parse_target("Rename instances in one explicit schematic.", destructive=False)
    client = VirtuosoClient.from_env()
    cv = (f'dbOpenCellViewByType("{escape_skill_string(args.lib)}" '
          f'"{escape_skill_string(args.cell)}" "schematic" nil "a")')

    for old, new in RENAMES:
        r = client.execute_skill(f"""
let((cv inst)
  cv = {cv}
  inst = car(setof(x cv~>instances x~>name == "{old}"))
  when(inst inst~>name = "{new}" sprintf(nil "renamed: {old} -> {new}")))
""")
        print(r.output or f"  {old}: not found")

    elapsed, r = timed_call(lambda: client.execute_skill(
        f'let((cv) cv = {cv} schCheck(cv) dbSave(cv) "saved")'))
    print(f"[save] [{format_elapsed(elapsed)}]")
    return 0 if r.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

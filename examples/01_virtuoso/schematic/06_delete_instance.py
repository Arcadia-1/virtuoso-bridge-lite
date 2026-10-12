#!/usr/bin/env python3
"""Delete one instance from an explicit schematic.

Usage::

    python 06_delete_instance.py <LIB> <CELL> [--instance NAME] [--yes]

Without --yes it only reports which instance it would delete.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from _target import parse_target
from virtuoso_bridge import VirtuosoClient, decode_skill_output
from virtuoso_bridge.virtuoso.ops import escape_skill_string


def main() -> int:
    args = parse_target(
        "Delete one instance from an explicit schematic.", destructive=True,
        extra=lambda p: p.add_argument("--instance", help="instance to delete (default: the first)"),
    )
    client = VirtuosoClient.from_env()
    cv = (f'dbOpenCellViewByType("{escape_skill_string(args.lib)}" '
          f'"{escape_skill_string(args.cell)}" "schematic" nil "a")')

    r = client.execute_skill(f"""
let((cv out)
  cv = {cv}
  out = ""
  foreach(inst cv~>instances
    out = strcat(out inst~>name "\\n"))
  out)
""")
    names = [n for n in decode_skill_output(r.output).splitlines() if n]
    print(f"Instances: {names}")
    target = args.instance or (names[0] if names else None)
    if target not in names:
        print(f"No instance {target!r} in {args.lib}/{args.cell}.")
        return 1
    if not args.yes:
        print(f"Would delete {target} from {args.lib}/{args.cell}; pass --yes to do it.")
        return 0

    elapsed, r = timed_call(lambda: client.execute_skill(f"""
let((cv inst)
  cv = {cv}
  inst = car(setof(x cv~>instances x~>name == "{escape_skill_string(target)}"))
  when(inst dbDeleteObject(inst))
  schCheck(cv) dbSave(cv)
  sprintf(nil "deleted: %s" "{escape_skill_string(target)}"))
"""))
    print(f"{r.output}  [{format_elapsed(elapsed)}]")
    return 0 if r.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Delete the first instance from the currently open schematic.

Run once per instance to delete them one at a time.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A schematic open in Virtuoso
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient


def main() -> int:
    client = VirtuosoClient.from_env()

    # List current instances
    r = client.execute_skill('''
let((cv out)
  cv = geGetEditCellView()
  out = ""
  foreach(inst cv~>instances
    out = strcat(out inst~>name "\\n"))
  out)
''')
    from virtuoso_bridge import decode_skill_output
    raw = decode_skill_output(r.output)
    names = [n for n in raw.splitlines() if n]
    print(f"Instances: {names}")

    if not names:
        print("No instances to delete.")
        return 0

    # Save, delete first instance, save again
    target = names[0]
    elapsed, r = timed_call(lambda: client.execute_skill(f'''
let((cv inst)
  cv = geGetEditCellView()
  schCheck(cv) dbSave(cv)
  inst = car(setof(x cv~>instances x~>name == "{target}"))
  when(inst dbDeleteObject(inst))
  schCheck(cv) dbSave(cv)
  sprintf(nil "deleted: {target}"))
'''))
    print(f"{r.output}  [{format_elapsed(elapsed)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

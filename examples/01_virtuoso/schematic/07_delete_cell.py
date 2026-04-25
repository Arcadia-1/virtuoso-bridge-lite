#!/usr/bin/env python3
"""Close and delete the currently open schematic cell.

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

    elapsed, r = timed_call(lambda: client.execute_skill('''
let((win lib cell ddcell)
  win = car(setof(w hiGetWindowList()
              w~>cellView && w~>cellView~>viewName == "schematic"))
  unless(win return("ERROR: no schematic window open"))
  lib = win~>cellView~>libName
  cell = win~>cellView~>cellName
  dbSave(win~>cellView)
  hiCloseWindow(win)
  ddcell = ddGetObj(lib cell)
  if(ddcell
    then ddDeleteObj(ddcell) sprintf(nil "deleted: %s/%s" lib cell)
    else sprintf(nil "ERROR: cell not found: %s/%s" lib cell)))
''', timeout=30))

    print(f"{r.output}  [{format_elapsed(elapsed)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

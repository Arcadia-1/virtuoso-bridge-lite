#!/usr/bin/env python3
"""List cells and views in a Virtuoso library.

Usage::

    python 04_list_library_cells.py              # list all library names
    python 04_list_library_cells.py MY_LIB       # list cells + views
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed
from virtuoso_bridge import VirtuosoClient


def main() -> int:
    client = VirtuosoClient.from_env()

    if len(sys.argv) < 2:
        r = client.execute_skill('''
let((out)
  out = ""
  foreach(lib ddGetLibList()
    out = strcat(out lib~>name "\\n"))
  out)
''', timeout=20)
        print(f"[list libraries] [{format_elapsed(r.execution_time or 0.0)}]")
        for lib in filter(None, decode_skill(r.output or "").splitlines()):
            print(f"  {lib}")
        return 0

    lib_name = sys.argv[1]
    r = client.execute_skill(f'''
let((lib out views)
  lib = ddGetObj("{lib_name}")
  out = ""
  when(lib
    foreach(cell lib~>cells
      views = ""
      foreach(view cell~>views
        views = strcat(views view~>name " "))
      out = strcat(out sprintf(nil "%s|views=%s\\n" cell~>name views))))
  out)
''', timeout=20)
    print(f"[list cells] [{format_elapsed(r.execution_time or 0.0)}]")
    for row in filter(None, decode_skill(r.output or "").splitlines()):
        cell, _, views = row.partition("|views=")
        print(f"  {cell:<20} [{views.strip()}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

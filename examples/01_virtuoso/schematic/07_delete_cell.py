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

from _timing import format_elapsed
from virtuoso_bridge import VirtuosoClient

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "schematic_ops.il"


def _decode(raw: str) -> str:
    text = (raw or "").strip().strip('"')
    return text.replace("\\n", "\n").replace('\\"', '"')


def main() -> int:
    client = VirtuosoClient.from_env()

    load_result = client.load_il(IL_FILE)
    print(f"[load_il] {'uploaded' if load_result.metadata.get('uploaded') else 'cache hit'}"
          f"  [{format_elapsed(load_result.execution_time or 0.0)}]")

    # Get lib/cell from the open window, then delete
    skill = (
        'let((win lib cell) '
        'win = car(setof(w hiGetWindowList() w~>cellView && w~>cellView~>viewName == "schematic")) '
        'unless(win return("ERROR: no schematic window open")) '
        'lib = win~>cellView~>libName '
        'cell = win~>cellView~>cellName '
        'SchDeleteCell(lib cell))'
    )

    result = client.execute_skill(skill, timeout=30)
    print(f"[execute_skill] [{format_elapsed(result.execution_time or 0.0)}]")
    output = _decode(result.output or "")
    errors = result.errors or []
    if output:
        print(output)
    for e in errors:
        print(f"[error] {e}")
    if not output and not errors:
        print(f"[status] {result.status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

    # Get current instance list
    result = client.execute_skill("SchListInsts()", timeout=15)
    names = [n for n in _decode(result.output or "").splitlines() if n]
    print(f"[SchListInsts] {names}  [{format_elapsed(result.execution_time or 0.0)}]")

    if not names:
        print("No instances to delete.")
        return 0

    # Save first (pre-flight checkpoint), then delete and save again
    target = names[0]
    result = client.execute_operations(
        ["SchSave()", f'SchDeleteInst("{target}")', "SchSave()"],
        timeout=30,
    )
    print(f"[execute_operations] [{format_elapsed(result.execution_time or 0.0)}]")
    print(_decode(result.output or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

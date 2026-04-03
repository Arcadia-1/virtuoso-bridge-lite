#!/usr/bin/env python3
"""Rename instances in the currently open schematic, check and save.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A schematic open in Virtuoso (e.g. created by 11_schematic_create.py)
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

    renames = [("I0", "IAAA_RENAMED"), ("R0", "RBBB_RENAMED")]

    commands = [f'SchRenameInst("{old}" "{new}")' for old, new in renames]
    commands.append("SchSave()")

    result = client.execute_operations(commands, timeout=30)
    print(f"[execute_operations] [{format_elapsed(result.execution_time or 0.0)}]")
    print(_decode(result.output or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

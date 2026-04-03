#!/usr/bin/env python3
"""List cells and views in a Virtuoso library.

Usage::

    python 05_list_library_cells.py              # list all library names
    python 05_list_library_cells.py PLAYGROUND_LLM
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed
from virtuoso_bridge import VirtuosoClient

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "list_library_cells.il"


def _decode(raw: str) -> str:
    text = (raw or "").strip().strip('"')
    return text.replace("\\n", "\n").replace('\\"', '"')


def main() -> int:
    client = VirtuosoClient.from_env()
    load_result = client.load_il(IL_FILE)
    upload_tag = "uploaded" if load_result.metadata.get("uploaded") else "cache hit"
    print(f"[load_il] {upload_tag}  [{format_elapsed(load_result.execution_time or 0.0)}]")

    if len(sys.argv) < 2:
        result = client.execute_skill("ListLibraries()", timeout=20)
        print(f"[execute_skill] [{format_elapsed(result.execution_time or 0.0)}]")
        for lib in filter(None, _decode(result.output or "").splitlines()):
            print(f"  {lib}")
        return 0

    lib_name = sys.argv[1]
    result = client.execute_skill(f'ListLibraryCells("{lib_name}")', timeout=20)
    print(f"[execute_skill] [{format_elapsed(result.execution_time or 0.0)}]")
    for row in filter(None, _decode(result.output or "").splitlines()):
        cell, _, views = row.partition("|views=")
        print(f"  {cell:<20} [{views.strip()}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create a demo schematic with idc + res instances connected via net labels.

Usage::

    python 03_create_rc.py [LIB]   # default: PLAYGROUND_LLM

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def main() -> int:
    lib  = sys.argv[1] if len(sys.argv) >= 2 else "PLAYGROUND_LLM"
    cell = f"tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    client = VirtuosoClient.from_env()

    load_elapsed, load_result = timed_call(lambda: client.load_il(ASSETS / "schematic_ops.il"))
    meta = load_result.metadata
    print(f"[load_il] {'uploaded' if meta.get('uploaded') else 'cache hit'}  [{format_elapsed(load_elapsed)}]")
    load_elapsed, load_result = timed_call(lambda: client.load_il(ASSETS / "create_rc.il"))
    meta = load_result.metadata
    print(f"[load_il] {'uploaded' if meta.get('uploaded') else 'cache hit'}  [{format_elapsed(load_elapsed)}]")
    print(f"Library : {lib}\nCell    : {cell}")

    exec_elapsed, result = timed_call(
        lambda: client.execute_skill(f'SchCreateRC("{lib}" "{cell}")', timeout=30)
    )
    print(f"[execute_skill] [{format_elapsed(exec_elapsed)}]")
    print(decode_skill(result.output or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

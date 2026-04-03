#!/usr/bin/env python3
"""Clear all currently visible figures from the active layout window."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, result = timed_call(lambda: client.layout.clear_current(timeout=30))
    print(f"[layout.clear_current] [{format_elapsed(elapsed)}]")

    output = decode_skill(result.output or "")
    if output:
        print(output)
    for error in result.errors or []:
        print(f"[error] {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

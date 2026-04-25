#!/usr/bin/env python3
"""Clear routing shapes from the current layout while keeping instances."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import layout_clear_routing


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, result = timed_call(
        lambda: client.execute_skill(layout_clear_routing(), timeout=30)
    )
    print(f"[layout_clear_routing] [{format_elapsed(elapsed)}]")
    print(decode_skill(result.output or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

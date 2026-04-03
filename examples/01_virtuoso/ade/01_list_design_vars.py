#!/usr/bin/env python3
"""List all ADE design variables from the current session.

Prerequisites:
- virtuoso-bridge tunnel running
- An ADE Explorer or Assembler session open in Virtuoso
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "ade_bridge.il"


def main() -> int:
    client = VirtuosoClient.from_env()

    load_elapsed, load_result = timed_call(lambda: client.load_il(IL_FILE))
    uploaded = "uploaded" if load_result.metadata.get("uploaded") else "cache hit"
    print(f"[load_il] {uploaded}  [{format_elapsed(load_elapsed)}]")

    elapsed, result = timed_call(lambda: client.execute_skill("adeBridgeListVars()"))
    print(f"[adeBridgeListVars] [{format_elapsed(elapsed)}]")

    if result.errors:
        print(f"Error: {result.errors[0]}")
        return 1

    print(f"\nDesign variables:")
    print(result.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

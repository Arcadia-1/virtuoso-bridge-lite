#!/usr/bin/env python3
"""Get and set a single ADE design variable.

Usage::

    python 02_get_set_var.py              # read VDD
    python 02_get_set_var.py VDD 0.85     # set VDD to 0.85
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "ade_bridge.il"


def main() -> int:
    var_name = sys.argv[1] if len(sys.argv) > 1 else "VDD"
    var_value = sys.argv[2] if len(sys.argv) > 2 else None

    client = VirtuosoClient.from_env()
    client.load_il(IL_FILE)

    if var_value:
        # Set
        elapsed, result = timed_call(
            lambda: client.execute_skill(f'adeBridgeSetVar("{var_name}" "{var_value}")')
        )
        print(f"[set] {var_name} = {var_value}  [{format_elapsed(elapsed)}]")
        if result.errors:
            print(f"Error: {result.errors[0]}")
            return 1

        # Read back
        elapsed, result = timed_call(
            lambda: client.execute_skill(f'adeBridgeGetVar("{var_name}")')
        )
        print(f"[verify] {var_name} = {result.output}  [{format_elapsed(elapsed)}]")
    else:
        # Get
        elapsed, result = timed_call(
            lambda: client.execute_skill(f'adeBridgeGetVar("{var_name}")')
        )
        print(f"{var_name} = {result.output}  [{format_elapsed(elapsed)}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

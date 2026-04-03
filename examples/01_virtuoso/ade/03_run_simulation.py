#!/usr/bin/env python3
"""Trigger ADE Explorer simulation and get results directory.

Prerequisites:
- An ADE Explorer window open and configured in Virtuoso
- Simulation setup ready (testbench, simulator, analyses configured)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "ade_bridge.il"


def main() -> int:
    client = VirtuosoClient.from_env()
    client.load_il(IL_FILE)

    # Check ADE window exists
    r = client.execute_skill("adeBridgeFindAdeWindow()")
    if not r.output or r.output.strip() == "nil":
        print("No ADE Explorer/Assembler window found. Open one in Virtuoso first.")
        return 1
    print(f"[ADE window] {r.output}")

    # Get current results dir (before sim)
    r = client.execute_skill("adeBridgeGetResultsDir()")
    print(f"[results dir] {r.output}")

    # Run simulation
    print("\n[run] Triggering ADE simulation...")
    elapsed, result = timed_call(lambda: client.execute_skill("adeBridgeRunSim()", timeout=300))
    print(f"[run] done  [{format_elapsed(elapsed)}]")

    if result.errors:
        print(f"Error: {result.errors[0]}")
        return 1

    print(f"Result: {result.output}")

    # Get results dir after sim
    r = client.execute_skill("adeBridgeGetResultsDir()")
    print(f"[results dir] {r.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

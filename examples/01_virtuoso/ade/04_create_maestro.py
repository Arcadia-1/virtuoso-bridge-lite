#!/usr/bin/env python3
"""Create a Maestro view for an open schematic cellview.

Demonstrates:
- Creating a maestro view from scratch
- Adding a test with tran analysis
- Setting design variables
- Adding signal outputs
- Saving the setup

Prerequisites:
- virtuoso-bridge tunnel running
- A schematic cellview open in Virtuoso
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient


def main() -> int:
    client = VirtuosoClient.from_env()

    # Get the currently open schematic
    r = client.execute_skill("geGetEditCellView()")
    if not r.output or r.output.strip() == "nil":
        print("No schematic cellview open. Open one in Virtuoso first.")
        return 1
    print(f"[cellview] {r.output}")

    r = client.execute_skill('geGetEditCellView()~>libName')
    lib = r.output.strip('"')
    r = client.execute_skill('geGetEditCellView()~>cellName')
    cell = r.output.strip('"')
    print(f"[design] {lib} / {cell}")

    # Check if maestro view already exists
    r = client.execute_skill(
        f'member("maestro" dbAllCellViews(ddGetObj("{lib}") "{cell}"))'
    )
    if r.output and r.output.strip() != "nil":
        print(f"Maestro view already exists for {lib}/{cell}. Opening in read mode.")
        client.execute_skill(
            f'deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "r")'
        )
        return 0

    # Create maestro view
    print("\n[create] Creating maestro view...")
    elapsed, r = timed_call(lambda: client.execute_skill(
        f'deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "w")'
    ))
    if r.errors:
        print(f"Error: {r.errors[0]}")
        return 1
    print(f"[create] done  [{format_elapsed(elapsed)}]")

    # Get session handle
    # The session is stored on the maestro cellview
    test_name = "TRAN"

    # Add test with tran analysis
    print(f"[test] Adding test '{test_name}'...")
    r = client.execute_skill(
        f'let((maeCV ses) '
        f'maeCV = deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "r") '
        f'ses = maeCV~>davSession '
        f'maeCreateTest("{test_name}" ?lib "{lib}" ?cell "{cell}" '
        f'?view "schematic" ?simulator "spectre" ?session ses) '
        f')'
    )
    if r.errors:
        print(f"Error creating test: {r.errors[0]}")
        return 1
    print(f"[test] {r.output}")

    # Configure tran analysis
    print("[analysis] Setting tran analysis...")
    r = client.execute_skill(
        f'maeSetAnalysis("{test_name}" "tran" ?enable t '
        f'?options `(("stop" "10u") ("errpreset" "moderate")))'
    )
    print(f"[analysis] tran: {r.output}")

    # Add output
    print("[output] Adding outputs...")
    r = client.execute_skill(
        f'maeAddOutput("out" "{test_name}" ?outputType "net" ?signalName "/OUT")'
    )
    print(f"[output] {r.output}")

    # Save
    print("[save] Saving maestro setup...")
    r = client.execute_skill(
        f'let((maeCV ses) '
        f'maeCV = deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "r") '
        f'ses = maeCV~>davSession '
        f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" ?view "maestro" ?session ses) '
        f')'
    )
    if r.errors:
        print(f"Error saving: {r.errors[0]}")
        return 1
    print("[save] done")

    print(f"\nMaestro view created: {lib} / {cell} / maestro")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

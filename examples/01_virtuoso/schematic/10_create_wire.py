#!/usr/bin/env python3
"""Create a schematic with direct wires using wire creation APIs.

Demonstrates two ways to draw wires:
  - schematic_create_wire() — arbitrary polyline through explicit points
  - schematic_create_wire_between_instance_terms() — auto-wire two terminals

NOTE: Wire shapes alone have no electrical meaning — terminals must be
connected to the same named net to carry current.  This example pairs
wire drawing with net labels so the circuit is both visually wired and
electrically connected.

Circuit: RC filter — VDC → R0 → C0 → GND, with IN/OUT pins.

Usage::

    python 10_create_wire.py <LIB>
    python 10_create_wire.py <LIB> <CELL>

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - analogLib cell masters (vdc, res, cap) available
"""

from __future__ import annotations

import sys
from datetime import datetime

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_inst_by_master_name as inst,
    schematic_create_pin,
    schematic_create_wire,
    schematic_create_wire_between_instance_terms,
    schematic_label_instance_term as label_term,
)


def _create(client: VirtuosoClient, lib: str, cell: str) -> None:
    with client.schematic.edit(lib, cell) as sch:
        # Place instances in a row
        sch.add(inst("analogLib", "vdc", "symbol", "V0", 0.0, 0.0, "R0"))
        sch.add(inst("analogLib", "res", "symbol", "R0", 3.0, 0.0, "R0"))
        sch.add(inst("analogLib", "cap", "symbol", "C0", 6.0, 0.0, "R0"))

        # --- Physical wires ---
        # Draw wires between terminals; coordinates auto-calculated from
        # the terminal geometry of each component.
        sch.add(schematic_create_wire_between_instance_terms("V0", "PLUS", "R0", "PLUS"))
        sch.add(schematic_create_wire_between_instance_terms("R0", "MINUS", "C0", "PLUS"))

        # --- GND path: explicit polyline ---
        # V0 MINUS and C0 MINUS are both at x=0 and x=6 respectively.
        # Route a horizontal wire at y=-1.5 to bridge them.
        sch.add(schematic_create_wire([
            (0.0, -1.5),
            (6.0, -1.5),
        ]))

        # --- Electrical binding via net labels ---
        # Wire shapes are purely graphical.  To give terminals a net name
        # (so the netlister knows they are connected), place net labels.
        sch.add(label_term("V0", "PLUS",  "VDD"))
        sch.add(label_term("V0", "MINUS", "GND"))
        sch.add(label_term("R0", "PLUS",  "VDD"))
        sch.add(label_term("R0", "MINUS", "OUT"))
        sch.add(label_term("C0", "PLUS",  "OUT"))
        sch.add(label_term("C0", "MINUS", "GND"))

        # --- Pins at key nets ---
        sch.add(schematic_create_pin("IN",  1.5, 0.75, "R0", direction="input"))
        sch.add(schematic_create_pin("OUT", 4.5, 0.75, "R0", direction="output"))

    # Set VDC = 1.0 V
    client.execute_skill(
        'schHiReplace(?replaceAll t ?propName "cellName" ?condOp "==" '
        '?propValue "vdc" ?newPropName "vdc" ?newPropValue "1.0")')

    client.open_window(lib, cell, view="schematic")
    print(f"Created {lib}/{cell}/schematic")
    print("Wire shapes drawn by schCreateWire; nets bound by net labels")


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: python {__file__} <LIB> [CELL]", file=sys.stderr)
        return 1

    lib = sys.argv[1]
    cell = (
        sys.argv[2]
        if len(sys.argv) >= 3
        else f"wire_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )

    client = VirtuosoClient.from_env()
    _create(client, lib, cell)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

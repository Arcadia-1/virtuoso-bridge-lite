#!/usr/bin/env python3
"""Create a schematic with direct wires using wire creation APIs.

Demonstrates two ways to draw wires plus three label-cosmetic patterns
that make the resulting schematic readable:

  Wire APIs:
    - schematic_create_wire() — arbitrary polyline through explicit points
    - schematic_create_wire_between_instance_terms() — auto-wire two terminals

  Label patterns (`_clean_label` helper below + offset/branch demo):
    1. Cleaner ``label_term`` defaults — ``extension_length=0.5`` and
       ``justification="lowerCenter"`` keep the text adjacent to the
       wire instead of overlapping the stub.  Bare defaults
       (``extension_length=0.25``, ``"centerCenter"``) leave the text
       sitting on top of the wire stub which renders unreadably on
       small symbols.
    2. Auto-rotation — pick label rotation from the instance rotation:
       R0/R180 instance → horizontal stub → label rotation ``R0``;
       R90/R270 instance → vertical stub → label rotation ``R90``.
       The example RC uses only R0 instances, but the helper handles
       both so it copy-pastes safely.
    3. Off-wire branch label — when the main stub is too crowded to
       carry the label cleanly, draw a perpendicular branch wire
       (electrically equivalent) and place the label at the branch
       tip.  Demonstrated here for the OUT scope point.

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


def _clean_label(
    instance: str,
    term: str,
    net: str,
    *,
    instance_rotation: str = "R0",
) -> str:
    """``label_term`` with cosmetic defaults that don't overlap the stub.

    ``extension_length=0.5`` lengthens the auto-placed stub so the label
    midpoint clears the symbol; ``justification="lowerCenter"`` anchors
    the text below (for horizontal stubs) or to the left (for vertical
    stubs) instead of dead-centred over the wire.

    ``instance_rotation`` is the orientation of the *instance* whose
    terminal we're labelling — R0/R180 give horizontal stubs (label
    rotation R0); R90/R270 give vertical stubs (label rotation R90).
    Pass the same string you used in ``schematic_create_inst_*``.
    """
    label_rot = "R90" if instance_rotation in ("R90", "R270") else "R0"
    return label_term(
        instance, term, net,
        extension_length=0.5,
        justification="lowerCenter",
        rotation=label_rot,
    )


def _create(client: VirtuosoClient, lib: str, cell: str) -> None:
    with client.schematic.edit(lib, cell) as sch:
        # Place instances in a row (all R0).
        sch.add(inst("analogLib", "vdc", "symbol", "V0", 0.0, 0.0, "R0"))
        sch.add(inst("analogLib", "res", "symbol", "R0", 3.0, 0.0, "R0"))
        sch.add(inst("analogLib", "cap", "symbol", "C0", 6.0, 0.0, "R0"))

        # --- Physical wires ---
        # Coordinates auto-calculated from each component's terminal geometry.
        sch.add(schematic_create_wire_between_instance_terms("V0", "PLUS", "R0", "PLUS"))
        sch.add(schematic_create_wire_between_instance_terms("R0", "MINUS", "C0", "PLUS"))

        # --- GND path: explicit polyline ---
        # V0 MINUS at x=0, C0 MINUS at x=6.  Bridge with a horizontal wire at y=-1.5.
        sch.add(schematic_create_wire([(0.0, -1.5), (6.0, -1.5)]))

        # --- Electrical binding via net labels (clean defaults) ---
        # Pattern (1) + (2): cleaner cosmetic params + auto-rotation
        # picked from each instance's orientation.  All R0 here, so all
        # labels come out R0.
        sch.add(_clean_label("V0", "PLUS",  "VDD", instance_rotation="R0"))
        sch.add(_clean_label("V0", "MINUS", "GND", instance_rotation="R0"))
        sch.add(_clean_label("R0", "PLUS",  "VDD", instance_rotation="R0"))
        sch.add(_clean_label("R0", "MINUS", "OUT", instance_rotation="R0"))
        sch.add(_clean_label("C0", "PLUS",  "OUT", instance_rotation="R0"))
        sch.add(_clean_label("C0", "MINUS", "GND", instance_rotation="R0"))

        # --- Pattern (3): off-wire branch label for an OUT scope point ---
        # The main OUT wire (R0/MINUS → C0/PLUS) is already labelled at
        # both ends.  Suppose we want a separate "SCOPE" tap that's
        # visually disjoint from those labels, e.g. for a probe point
        # routed up on the schematic.  Recipe:
        #   1. Draw a perpendicular branch wire from a point on the
        #      main wire up to a clear area.
        #   2. Place a label at the branch tip (electrically still on
        #      the same net via the branch wire — net solver merges).
        # Main OUT wire runs at y=0 from x=4 (R0 MINUS) to x=6 (C0
        # PLUS); pick x=5 as the tap point and route up to y=1.5.
        sch.add(schematic_create_wire([(5.0, 0.0), (5.0, 1.5)]))
        # Plain ``schCreateWireLabel``-equivalent at the tip — net name
        # alone is enough; the branch wire carries the connection.
        # Using bare ``label_term`` here would re-stub at C0/PLUS; for
        # an arbitrary midpoint we drop a bare wire-label via SKILL.
        sch.add(
            'schCreateWireLabel(cv nil list(5.0 1.7) "OUT" '
            '"lowerCenter" "R0" "stick" 0.0625 nil)'
        )

        # --- Pins at key nets ---
        sch.add(schematic_create_pin("IN",  1.5, 0.75, "R0", direction="input"))
        sch.add(schematic_create_pin("OUT", 4.5, 0.75, "R0", direction="output"))

    # Set VDC = 1.0 V
    client.execute_skill(
        'schHiReplace(?replaceAll t ?propName "cellName" ?condOp "==" '
        '?propValue "vdc" ?newPropName "vdc" ?newPropValue "1.0")')

    client.open_window(lib, cell, view="schematic")
    print(f"Created {lib}/{cell}/schematic")
    print("Wires drawn by schCreateWire; nets bound by clean labels;")
    print("OUT also has a branch-tip SCOPE label demonstrating the off-wire pattern.")


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

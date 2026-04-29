#!/usr/bin/env python3
"""Create a schematic with explicit pins using pin creation APIs.

Demonstrates two ways to add pins:
  - schematic_create_pin() — place a pin at an explicit (x, y)
  - schematic_create_pin_at_instance_term() — place a pin at an instance terminal

Circuit: voltage divider with VDD, GND (input pins) and OUT (output pin).

Usage::

    python 09_create_pins.py <LIB>
    python 09_create_pins.py <LIB> <CELL>

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - analogLib cell masters (res) available
"""

from __future__ import annotations

import sys
from datetime import datetime

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_inst_by_master_name as inst,
    schematic_create_pin,
    schematic_create_pin_at_instance_term,
)


def _create(client: VirtuosoClient, lib: str, cell: str) -> None:
    with client.schematic.edit(lib, cell) as sch:
        # Two resistors in series forming a voltage divider
        sch.add(inst("analogLib", "res", "symbol", "R0", 0.0, 0.5, "R0"))
        sch.add(inst("analogLib", "res", "symbol", "R1", 0.0, -0.5, "R0"))

        # --- Method 1: explicit position pins ---
        # VDD input pin placed above the top resistor
        sch.add(schematic_create_pin("VDD", 0.0, 1.5, "R0", direction="input"))
        # GND input pin placed below the bottom resistor
        sch.add(schematic_create_pin("GND", 0.0, -1.5, "R0", direction="input"))

        # --- Method 2: pin at instance terminal center ---
        # OUT output pin auto-positioned at R0's MINUS terminal
        sch.add(schematic_create_pin_at_instance_term(
            "R0", "MINUS", "OUT", direction="output",
        ))

    client.open_window(lib, cell, view="schematic")
    print(f"Created {lib}/{cell}/schematic")
    print("Pins: VDD (input), GND (input), OUT (output)")


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: python {__file__} <LIB> [CELL]", file=sys.stderr)
        return 1

    lib = sys.argv[1]
    cell = (
        sys.argv[2]
        if len(sys.argv) >= 3
        else f"pins_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )

    client = VirtuosoClient.from_env()
    _create(client, lib, cell)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

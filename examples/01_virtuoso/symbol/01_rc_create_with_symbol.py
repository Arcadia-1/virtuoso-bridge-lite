#!/usr/bin/env python3
"""Create an RC low-pass filter schematic and auto-generate its symbol view.

Two-step flow:

  1. Build a fresh RC filter schematic — series R + shunt C with three
     pins (IN, OUT, GND).  The cell is auto-timestamped so reruns never
     overwrite earlier results.

  2. Generate a symbol view through ``client.symbol.generate_from_schematic()``,
     which wraps the Text-to-Symbol Generator (TSG) primitives::

        schSchemToPinList(lib, cell, "schematic")    → pin-list struct
        schPinListToSymbol(lib, cell, "symbol", pl)  → writes the symbol view

     The helper uses a temporary symbol view, verifies its terminals, and then
     copies it into place without relying on GUI dialog dismissal.

Pin order on the generated symbol comes from the schematic env var
``ssgSortPins``:

  - ``alphanumeric`` (default) — sorted by pin name
  - ``geometric``              — preserves spatial layout from the schematic

We flip it to ``geometric`` so IN ends up on the left, OUT on the right,
and GND on the bottom — i.e. where they sit in the schematic.

Usage::

    python 01_rc_create_with_symbol.py <LIB>

Example::

    python 01_rc_create_with_symbol.py PLAYGROUND_LLM
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_inst_by_master_name as inst,
    schematic_create_pin_at_instance_term as pin_at,
    schematic_create_wire_between_instance_terms as wire,
)


def _create_schematic(client: VirtuosoClient, lib: str, cell: str) -> None:
    """RC filter: series R from IN→OUT, shunt C from OUT→GND."""
    with client.schematic.edit(lib, cell) as sch:
        # R0 horizontal between IN (left) and OUT (right).
        sch.add(inst("analogLib", "res", "symbol", "R0", 0.5, 0.0, "R90"))
        # C0 vertical between OUT (top) and GND (bottom).
        sch.add(inst("analogLib", "cap", "symbol", "C0", 1.5, -0.5, "R0"))
        # Wire R0/MINUS  →  C0/PLUS  (the OUT node).
        sch.add(wire("R0", "MINUS", "C0", "PLUS"))
        # Three external pins, placed at the instance terminals so they pick
        # up connectivity automatically.
        sch.add(pin_at("R0", "PLUS",  "IN",  direction="input"))
        sch.add(pin_at("C0", "PLUS",  "OUT", direction="output"))
        sch.add(pin_at("C0", "MINUS", "GND", direction="inputOutput"))
        # schCheck + dbSave run on context exit.


def _generate_symbol(client: VirtuosoClient, lib: str, cell: str) -> None:
    """Run TSG: schematic → pin list → symbol view."""
    client.symbol.generate_from_schematic(
        lib,
        cell,
        sort_pins="geometric",
    )


def _verify_views(client: VirtuosoClient, lib: str, cell: str) -> list[str]:
    r = client.execute_skill(f'ddGetObj("{lib}" "{cell}")~>views~>name')
    return re.findall(r'"([^"]+)"', r.output or "")


def main() -> int:
    if len(sys.argv) < 2:
        print("=" * 60, file=sys.stderr)
        print(" ERROR: missing required argument <LIB>", file=sys.stderr)
        print()
        print(
            f" Usage: python {Path(__file__).name} <LIB>\n"
            " Example: python 01_rc_create_with_symbol.py PLAYGROUND_LLM\n",
            file=sys.stderr,
        )
        print("=" * 60, file=sys.stderr)
        return 1

    lib = sys.argv[1]
    cell = f"RC_FILTER_{datetime.now():%Y%m%d_%H%M%S}"
    client = VirtuosoClient.from_env()

    print(f"[info] target: {lib}/{cell}")

    _create_schematic(client, lib, cell)
    print(f"[schematic] {lib}/{cell}/schematic — R0, C0, pins (IN, OUT, GND)")

    _generate_symbol(client, lib, cell)
    print(f"[symbol]    {lib}/{cell}/symbol — generated via TSG (geometric)")

    views = _verify_views(client, lib, cell)
    print(f"[verify]    views: {views}")
    if "schematic" not in views or "symbol" not in views:
        print("[ERROR] expected both 'schematic' and 'symbol' views", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

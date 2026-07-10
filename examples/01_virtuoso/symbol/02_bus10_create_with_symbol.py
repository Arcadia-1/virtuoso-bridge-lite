#!/usr/bin/env python3
"""Create a 10-input / 10-output schematic and auto-generate its symbol view.

Scales up the TSG flow from ``01_rc_create_with_symbol.py`` to 20 pins.
Topology: ten independent resistor passthroughs R0..R9, each carrying
one IN<i> → OUT<i> channel.  The instances are stacked vertically with
PLUS on the left and MINUS on the right, so that ``ssgSortPins =
"geometric"`` produces the natural symbol layout — 10 input pins down
the left side, 10 output pins down the right side, top-to-bottom in
index order.

This stresses the symbol generator with a wider pin count and verifies
that bus-style cells round-trip cleanly.

Usage::

    python 02_bus10_create_with_symbol.py <LIB>

Example::

    python 02_bus10_create_with_symbol.py PLAYGROUND_LLM
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
)


N_CHANNELS = 10
ROW_PITCH = 1.0  # vertical spacing between channels (schematic units)


def _create_schematic(client: VirtuosoClient, lib: str, cell: str) -> None:
    """10 horizontal resistors stacked vertically; one IN<i>/OUT<i> pair each."""
    with client.schematic.edit(lib, cell) as sch:
        for i in range(N_CHANNELS):
            y = -i * ROW_PITCH
            # R<i> oriented R90 → PLUS on left at x≈0.0, MINUS on right at x≈1.0.
            sch.add(inst("analogLib", "res", "symbol", f"R{i}", 0.5, y, "R90"))
            # Pins at instance terminals — auto-positioned, no separate wires.
            sch.add(pin_at(f"R{i}", "PLUS",  f"IN{i}",  direction="input"))
            sch.add(pin_at(f"R{i}", "MINUS", f"OUT{i}", direction="output"))
        # schCheck + dbSave run on context exit.


def _generate_symbol(client: VirtuosoClient, lib: str, cell: str) -> None:
    # Geometric sort → pin order on the symbol mirrors schematic position
    # (top-to-bottom).  Default alphanumeric would sort IN10 before IN2 etc.
    client.symbol.generate_from_schematic(
        lib,
        cell,
        sort_pins="geometric",
    )


def _verify(client: VirtuosoClient, lib: str, cell: str) -> tuple[list[str], list[str]]:
    """Return (views, symbol_pin_names)."""
    r = client.execute_skill(f'ddGetObj("{lib}" "{cell}")~>views~>name')
    views = re.findall(r'"([^"]+)"', r.output or "")
    # Read the generated symbol's pin list to confirm all 20 made it.
    r = client.execute_skill(
        f'let((cv) '
        f'cv = dbOpenCellViewByType("{lib}" "{cell}" "symbol" nil "r") '
        f'when(cv cv~>terminals~>name))'
    )
    pins = re.findall(r'"([^"]+)"', r.output or "")
    return views, pins


def main() -> int:
    if len(sys.argv) < 2:
        print("=" * 60, file=sys.stderr)
        print(" ERROR: missing required argument <LIB>", file=sys.stderr)
        print()
        print(
            f" Usage: python {Path(__file__).name} <LIB>\n"
            " Example: python 02_bus10_create_with_symbol.py PLAYGROUND_LLM\n",
            file=sys.stderr,
        )
        print("=" * 60, file=sys.stderr)
        return 1

    lib = sys.argv[1]
    cell = f"BUS10_{datetime.now():%Y%m%d_%H%M%S}"
    client = VirtuosoClient.from_env()

    print(f"[info] target: {lib}/{cell}")

    _create_schematic(client, lib, cell)
    print(f"[schematic] {lib}/{cell}/schematic — R0..R{N_CHANNELS-1}, "
          f"{N_CHANNELS} IN + {N_CHANNELS} OUT")

    _generate_symbol(client, lib, cell)
    print(f"[symbol]    {lib}/{cell}/symbol — generated via TSG (geometric)")

    views, pins = _verify(client, lib, cell)
    print(f"[verify]    views: {views}")
    print(f"[verify]    pins ({len(pins)}): {pins}")

    expected_n = 2 * N_CHANNELS
    if "schematic" not in views or "symbol" not in views:
        print("[ERROR] expected both 'schematic' and 'symbol' views", file=sys.stderr)
        return 1
    if len(pins) != expected_n:
        print(f"[ERROR] expected {expected_n} pins, got {len(pins)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

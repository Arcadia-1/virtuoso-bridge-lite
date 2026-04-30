#!/usr/bin/env python3
"""Create a Verilog-A cellview from a local ``.va`` file.

The IC618 path from "I have a .va on disk" to "Virtuoso recognises it as
a behavioural cellview" is **five steps**, none of which the bridge
exposes as a single high-level helper today (this example is the helper):

1. **Placeholder schematic** with floating pins matching the .va ports.
   Cadence's symbol generator works off the schematic's pin list, not
   off the .va text — so we need a schematic first, even though it
   never gets simulated.
2. **Symbol** auto-generated from the schematic via
   ``schSchemToPinList`` + ``schPinListToSymbol``. Geometric pin sort
   so the symbol layout follows the schematic's pin placement.
   (Do **not** use ``schPinListToSymbolGen`` — it builds an
   empty-terminal symbol that the veriloga generator chokes on.)
3. **Veriloga skeleton** generated from the symbol via
   ``schViewToView ... "symbol" "veriloga"
   "schSymbolToPinList" "ahdlPinListToveriloga"``. This drops a
   default-content ``veriloga.va`` into ``<lib>/<cell>/veriloga/``.
4. **Overwrite the skeleton** with the user's real ``.va`` — uploaded
   via ``client.upload_file`` to ``<readPath>/<cell>/veriloga/veriloga.va``.
5. **Reparse** with ``ahdlUpdateViewInfo`` so Virtuoso picks up the
   new contents (without this, the skeleton stays cached and the cell
   netlists with the wrong ports).

After step 5 the cell is ready to be instantiated in another schematic
just like a stdcell or an analogLib master.

Usage::

    python import_veriloga.py <LIB> <CELL> <local-.va-file> \\
                              --inputs IN1 IN2 \\
                              --outputs OUT1

Pin direction is taken from the CLI flags; the ``.va`` declarations
must agree (Cadence cross-checks).  ``--inout`` also accepted.

Example::

    python import_veriloga.py PLAYGROUND_LLM sample sample.va \\
                              --inout in out

PDK independence:
  Nothing in this script is TSMC- or N28-specific.  The placeholder
  schematic uses no analogLib masters at all — just floating pins —
  so it works on any tech library Virtuoso supports.

IC release dependency:
  Tested on IC618 SP201.  The ``schViewToView`` argument names
  (``"schSymbolToPinList"`` / ``"ahdlPinListToveriloga"``) are
  Cadence-defined and may rename across major IC releases; if a
  future release changes them, fix the constants below.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.ops import schematic_create_pin


# Cadence-defined view-to-view generator names. Renamed across IC
# major versions; current values are correct on IC618 SP201.
_SCHEMA_TO_PINLIST = "schSymbolToPinList"
_PINLIST_TO_VERILOGA = "ahdlPinListToveriloga"


def _build_placeholder_schematic(
    client: VirtuosoClient,
    lib: str,
    cell: str,
    inputs: list[str],
    outputs: list[str],
    inouts: list[str],
) -> None:
    """One floating pin per port; geometric placement so the symbol
    generator gets a sensible left/right/top/bottom split.

    Pin coordinate convention: inputs on the left (x=0), outputs on the
    right (x=4), inouts in the middle (x=2). Vertical stacks within
    each side.  ``schSchemToPinList`` reads the pin geometry to assign
    sides on the symbol when ``ssgSortPins == "geometric"``.
    """
    with client.schematic.edit(lib, cell) as sch:
        for i, name in enumerate(inputs):
            sch.add(schematic_create_pin(name, 0.0, -i * 1.0, direction="input"))
        for i, name in enumerate(outputs):
            sch.add(schematic_create_pin(name, 4.0, -i * 1.0, direction="output"))
        for i, name in enumerate(inouts):
            sch.add(schematic_create_pin(name, 2.0, -i * 1.0, direction="inputOutput"))


def _generate_symbol(client: VirtuosoClient, lib: str, cell: str) -> None:
    client.execute_skill('schSetEnv("ssgSortPins" "geometric")')
    r = client.execute_skill(
        'let((pl) '
        f'pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
        f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))'
    )
    if r.errors:
        raise RuntimeError(f"symbol generation failed: {r.errors[0]}")


def _generate_veriloga_skeleton(
    client: VirtuosoClient, lib: str, cell: str
) -> None:
    """Drops a stub ``veriloga.va`` under ``<lib>/<cell>/veriloga/``."""
    r = client.execute_skill(
        f'schViewToView("{lib}" "{cell}" nil nil "symbol" "veriloga" '
        f'"{_SCHEMA_TO_PINLIST}" "{_PINLIST_TO_VERILOGA}")'
    )
    if r.errors or not r.output or r.output.strip() in ("nil", ""):
        raise RuntimeError(f"veriloga skeleton generation failed: {r.errors}")


def _veriloga_remote_path(client: VirtuosoClient, lib: str, cell: str) -> str:
    """``<lib readPath>/<cell>/veriloga/veriloga.va`` on the remote."""
    r = client.execute_skill(f'ddGetObj("{lib}")~>readPath')
    out = (r.output or "").strip().strip('"')
    if not out or out == "nil":
        raise RuntimeError(f"could not resolve readPath for library {lib!r}")
    return f"{out}/{cell}/veriloga/veriloga.va"


def _overwrite_veriloga(
    client: VirtuosoClient, local_va: Path, remote_va: str
) -> None:
    """Upload local .va over the auto-skeleton."""
    client.upload_file(local_va, remote_va)


def _reparse(client: VirtuosoClient, lib: str, cell: str) -> None:
    r = client.execute_skill(
        f'ahdlUpdateViewInfo(?lib "{lib}" ?cell "{cell}" ?view "veriloga")'
    )
    if r.errors:
        raise RuntimeError(f"ahdlUpdateViewInfo failed: {r.errors[0]}")


def _verify(client: VirtuosoClient, lib: str, cell: str) -> list[str]:
    r = client.execute_skill(f'ddGetObj("{lib}" "{cell}")~>views~>name')
    import re

    return re.findall(r'"([^"]+)"', r.output or "")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Import a local .va as a Cadence Verilog-A cellview"
    )
    p.add_argument("lib", help="target OA library (must be in cds.lib)")
    p.add_argument("cell", help="target cell name (created if absent)")
    p.add_argument("va_file", type=Path, help="local .va file to import")
    p.add_argument(
        "--inputs", nargs="*", default=[], help="input port names (in order)"
    )
    p.add_argument(
        "--outputs", nargs="*", default=[], help="output port names (in order)"
    )
    p.add_argument(
        "--inout",
        "--inouts",
        nargs="*",
        default=[],
        dest="inouts",
        help="inout port names (in order)",
    )
    args = p.parse_args()

    if not args.va_file.exists():
        print(f"error: .va file not found: {args.va_file}", file=sys.stderr)
        return 1
    if not (args.inputs or args.outputs or args.inouts):
        print("error: at least one of --inputs/--outputs/--inout required",
              file=sys.stderr)
        return 1

    client = VirtuosoClient.from_env()

    print(f"[1/5] placeholder schematic — {args.lib}/{args.cell}")
    _build_placeholder_schematic(
        client, args.lib, args.cell,
        args.inputs, args.outputs, args.inouts,
    )

    print(f"[2/5] symbol via schPinListToSymbol")
    _generate_symbol(client, args.lib, args.cell)

    print(f"[3/5] veriloga skeleton via schViewToView")
    _generate_veriloga_skeleton(client, args.lib, args.cell)

    remote_va = _veriloga_remote_path(client, args.lib, args.cell)
    print(f"[4/5] overwrite skeleton ← {args.va_file}  →  {remote_va}")
    _overwrite_veriloga(client, args.va_file, remote_va)

    print(f"[5/5] ahdlUpdateViewInfo")
    _reparse(client, args.lib, args.cell)

    views = _verify(client, args.lib, args.cell)
    print(f"\nDone. {args.lib}/{args.cell} now has views: {views}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

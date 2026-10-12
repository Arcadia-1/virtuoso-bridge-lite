#!/usr/bin/env python3
"""Import a 4-bit binary-weighted capacitor array into Virtuoso from CDL.

Writes a CDL netlist and a device map locally and imports the hierarchy with
``client.schematic.import_netlist`` (Cadence spiceIn run in a private
directory whose cds.lib includes the CIW's, so technology bindings resolve).

Circuit:
    cap_unit:     single 10fF capacitor (TOP, BOT)
    cap_array_4b: 15 unit caps, weights [1,2,4,8], pins TOP + BOT<3:0>

Usage::

    python 08_import_cdl_cap_array.py <LIB>

    <LIB> is required — the Virtuoso library where the schematic will be created.

    Example::
    python 08_import_cdl_cap_array.py testlib

Prerequisites:
- virtuoso-bridge tunnel running
- spiceIn available in the Cadence installation

Note:
    Do not run spiceIn by hand from the library's parent directory: without
    the CIW's cds.lib it cannot resolve the library's technology and writes an
    empty tech.db into the library, which breaks later XStream exports.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from virtuoso_bridge import VirtuosoClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CDL = """\
.SUBCKT cap_unit TOP BOT
C0 TOP BOT cap C=1.0000e-14
.ENDS

.SUBCKT cap_array_4b TOP BOT<3:0>
XC_b0_0 TOP BOT<0> cap_unit
XC_b1_0 TOP BOT<1> cap_unit
XC_b1_1 TOP BOT<1> cap_unit
XC_b2_0 TOP BOT<2> cap_unit
XC_b2_1 TOP BOT<2> cap_unit
XC_b2_2 TOP BOT<2> cap_unit
XC_b2_3 TOP BOT<2> cap_unit
XC_b3_0 TOP BOT<3> cap_unit
XC_b3_1 TOP BOT<3> cap_unit
XC_b3_2 TOP BOT<3> cap_unit
XC_b3_3 TOP BOT<3> cap_unit
XC_b3_4 TOP BOT<3> cap_unit
XC_b3_5 TOP BOT<3> cap_unit
XC_b3_6 TOP BOT<3> cap_unit
XC_b3_7 TOP BOT<3> cap_unit
.ENDS
"""

DEVMAP = """\
devselect := resistor res
devselect := capacitor cap
devselect := inductor ind
"""


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: python {Path(__file__).name} <LIB>", file=sys.stderr)
        return 1
    lib = sys.argv[1]
    client = VirtuosoClient.from_env()

    existing = [
        cell for cell in ("cap_unit", "cap_array_4b")
        if client.execute_skill(f'ddGetObj("{lib}" "{cell}")').output not in (None, "", "nil")
    ]
    if existing:
        print(f"{lib} already contains {existing}; delete them or choose another library.")
        return 1

    with tempfile.TemporaryDirectory(prefix="vb-cap-array-") as tmp:
        cdl_path = Path(tmp) / "cap_array_4b.cdl"
        devmap_path = Path(tmp) / "devmap.txt"
        cdl_path.write_text(CDL, encoding="utf-8")
        devmap_path.write_text(DEVMAP, encoding="utf-8")
        print(f"[cdl]\n{CDL}")

        # Each call converts one subcircuit (the cell argument) to a schematic;
        # import the leaf first so the array instantiates its schematic cell.
        for cell in ("cap_unit", "cap_array_4b"):
            result = client.schematic.import_netlist(
                lib, cell, cdl_path, language="SPICE", dev_map_file=devmap_path,
            )
            if getattr(result, "status", None) is not None and result.status.value != "success":
                print(f"[spiceIn] FAILED for {cell}: {result.errors}")
                return 1
    print("[spiceIn] OK")

    # spiceIn can report success while silently skipping a subckt (for
    # example one with bus ports), so check the database directly.
    missing = [
        cell for cell in ("cap_unit", "cap_array_4b")
        if client.execute_skill(f'ddGetObj("{lib}" "{cell}" "schematic")').output in (None, "", "nil")
    ]
    if missing:
        print(f"[spiceIn] FAILED: expected cells not created: {missing}")
        return 1

    # spiceIn also creates the symbol of every subcircuit it instantiates.
    ports = client.symbol.read_ports(lib, "cap_unit")
    print(f"[symbol] cap_unit pins: {[term['name'] for term in ports['terms']]}")

    client.open_window(lib, "cap_array_4b", view="schematic")
    print("[done] Opened cap_array_4b")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

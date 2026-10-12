#!/usr/bin/env python3
"""Step 1: Create RC filter schematic + Maestro setup.

Creates a fresh, timestamped cell ``TB_RC_FILTER_<YYYYMMDD_HHMMSS>``:

- Schematic: vdc (AC=1) → R (1k) → C (c_val) → GND, with pin OUT
- Maestro: AC analysis 1Hz–10GHz, sweep c_val = 1p,100f, BW spec > 1GHz

We always create a new cell so reruns never overwrite a prior run's results.
The final cell name is printed at the end — pass it to ``06b_rc_simulate_and_read.py``.

Usage::

    python 06a_rc_create.py <LIB>

Example::

    python 06a_rc_create.py PLAYGROUND_LLM
    # → "[create] PLAYGROUND_LLM/TB_RC_FILTER_20260430_120000"
    # then:
    python 06b_rc_simulate_and_read.py PLAYGROUND_LLM TB_RC_FILTER_20260430_120000

Running this script from VSCode without passing <LIB> will NOT work.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient


def main() -> int:
    # ------------------------------------------------------------------
    # Argument check — this script MUST be run with a library argument.
    # ------------------------------------------------------------------
    if len(sys.argv) < 2:
        print("=" * 60, file=sys.stderr)
        print(" ERROR: missing required argument <LIB>", file=sys.stderr)
        print()
        print(
            f" Usage: python {Path(__file__).name} <LIB>\n"
            " Example: python 06a_rc_create.py lifangshi\n",
            file=sys.stderr,
        )
        print(
            " NOTE: Running this script from VSCode (Ctrl+F5 / F5) will NOT\n"
            "       work — VSCode does not pass command-line arguments by default.\n",
            file=sys.stderr,
        )
        print("=" * 60, file=sys.stderr)
        return 1

    lib = sys.argv[1]
    cell = f"TB_RC_FILTER_{datetime.now():%Y%m%d_%H%M%S}"

    client = VirtuosoClient.from_env()
    print(f"[info] {lib}/{cell}")

    # --- Create schematic ---
    print("[schematic] Creating RC filter...")
    from virtuoso_bridge.virtuoso.schematic import (
        schematic_create_inst_by_master_name as inst,
        schematic_create_wire_between_instance_terms as wire,
        schematic_create_pin_at_instance_term as pin_at,
    )
    with client.schematic.create(lib, cell) as sch:
        sch.add(inst("analogLib", "vdc", "symbol", "V0", 0, 0, "R0"))
        sch.add(inst("analogLib", "gnd", "symbol", "GND0", 0, -0.625, "R0"))
        sch.add(inst("analogLib", "res", "symbol", "R0", 1.5, 0.5, "R90"))
        sch.add(inst("analogLib", "cap", "symbol", "C0", 3.0, 0, "R0"))
        sch.add(inst("analogLib", "gnd", "symbol", "GND1", 3.0, -0.625, "R0"))
        sch.add(wire("V0", "PLUS", "R0", "PLUS"))
        sch.add(wire("R0", "MINUS", "C0", "PLUS"))
        sch.add(wire("C0", "MINUS", "GND1", "gnd!"))
        sch.add(wire("V0", "MINUS", "GND0", "gnd!"))
        sch.add(pin_at("C0", "PLUS", "OUT"))

    # Set CDF parameters
    cv = "_rcfCv"
    client.execute_skill(f'{cv} = dbOpenCellViewByType("{lib}" "{cell}" "schematic" nil "a")')
    for inst_, param, val in [("V0", "vdc", "0"), ("V0", "acm", "1"),
                              ("R0", "r", "1k"), ("C0", "c", "c_val")]:
        client.execute_skill(
            f'cdfFindParamByName(cdfGetInstCDF('
            f'car(setof(i {cv}~>instances i~>name == "{inst_}")))'
            f' "{param}")~>value = "{val}"')
    client.execute_skill(f"schCheck({cv})")
    client.execute_skill(f"dbSave({cv})")
    r = client.execute_skill(f"{cv}~>instances~>name")
    print(f"[schematic] {lib}/{cell}/schematic")
    print(f"  Instances: {r.output}")
    print(f"  V0: vdc=0, acm=1 | R0: r=1k | C0: c=c_val | Pin: OUT")

    # --- Create Maestro ---
    print("[maestro] Creating setup...")
    session = client.maestro.open_session(lib, cell)

    client.maestro.create_test("AC", lib=lib, cell=cell, session=session)
    client.maestro.set_analysis("AC", "tran", enable=False, session=session)
    client.maestro.set_analysis("AC", "ac",
                 options='(("start" "1") ("stop" "10G") '
                         '("incrType" "Logarithmic") ("stepTypeLog" "Points Per Decade") '
                         '("dec" "20"))',
                 session=session)
    client.maestro.add_output("Vout", "AC", output_type="net", signal_name="/OUT", session=session)
    client.maestro.add_output("BW", "AC", output_type="point",
               expr='bandwidth(mag(VF("/OUT")) 3 "low")', session=session)
    client.maestro.set_spec("BW", "AC", gt="1G", session=session)
    client.maestro.set_var("c_val", "1p,100f", session=session)

    client.maestro.save_setup(lib, cell, session=session)
    client.maestro.close_session(session)
    print(f"[maestro] {lib}/{cell}/maestro")
    print(f"  Test: AC | Analysis: ac 1Hz-10GHz, 20pts/dec")
    print(f"  Outputs: Vout (net /OUT), BW (bandwidth expr)")
    print(f"  Spec: BW > 1GHz")
    print(f"  Sweep: c_val = 1p, 100f")
    print()
    print(f"[next] python 06b_rc_simulate_and_read.py {lib} {cell}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

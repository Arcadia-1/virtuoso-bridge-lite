#!/usr/bin/env python3
"""Symbol Export: symbol generation → pin redistribution → pin extraction.

Stops after writing pin_info.json so the LLM can classify pins.
Run tb_builder.py after writing pin_classifications.json.

Usage:
    python scripts/symbol_export.py <lib> <cell> [--vdd <volts>]

Outputs:
    ${AMS_OUTPUT_ROOT}/simulation/<timestamp>/pin_info.json
    ${AMS_OUTPUT_ROOT}/simulation/<timestamp>/dut_context.json
    ${AMS_OUTPUT_ROOT}/simulation/.latest_run   (absolute path to the run directory)

Exit codes:
    0  success
    1  error (printed to stderr)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

_SIM_IO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SIM_IO))

from sim_io.flow import (
    DutContext,
    SimFlowResult,
    SKILL_DIR,
    create_run_dir,
    log_skill_code,
    export_symbol,
    redistribute_symbol,
    extract_dut_pins,
)
from sim_io.library_mapping import require_configured_library_mapping
from sim_io.pin_types import write_pin_info_json


def run_symbol_export(
    lib: str,
    primary_cell: str,
    *,
    vdd_value: float = 1.8,
    debug: bool = False,
    client: VirtuosoClient | None = None,
) -> DutContext:
    """Symbol export + redistribution + pin extraction.

    Steps:
      1. TSG: generate symbol view from schematic
      2. Redistribute symbol pins (extract geometry → compute layout → apply)
      3. Extract pin positions/directions from redistributed symbol
      4. Write pin_info.json → STOP for LLM classification
    """
    if client is None:
        client = VirtuosoClient.from_env()

    require_configured_library_mapping(client, lib)
    run_dir = create_run_dir()

    # Validate lib/cell
    r = client.execute_skill(f'ddGetObj("{lib}" "{primary_cell}")')
    if r.output and r.output.strip().lower() == "nil":
        r_libs = client.execute_skill('ddGetLibList()~>name')
        avail_libs = re.findall(r'"([^"]+)"', r_libs.output or "")
        libs_hint = ", ".join(avail_libs[:20]) if avail_libs else "(none found)"
        raise RuntimeError(
            f"Library/cell '{lib}/{primary_cell}' not found in Virtuoso. "
            f"Available libraries: {libs_hint}"
        )

    r_views = client.execute_skill(f'ddGetObj("{lib}" "{primary_cell}")~>views~>name')
    views = re.findall(r'"([^"]+)"', r_views.output or "")
    if "schematic" not in views:
        raise RuntimeError(
            f"Cell '{lib}/{primary_cell}' has no schematic view. "
            f"Available views: {views}"
        )

    for il_file in sorted(SKILL_DIR.glob("*.il")):
        log_skill_code(run_dir, il_file)

    print(f"\n{'='*60}")
    print(f" Symbol Export: {lib}/{primary_cell}  (VDD={vdd_value}V)")
    print(f" Output:  {run_dir}")
    print(f"{'='*60}\n")

    # Step 1: Export symbol (TSG)
    symbol_ok = export_symbol(client, lib, primary_cell)

    # Step 2: Redistribute symbol pins (extract → calculate → apply)
    redistributed = redistribute_symbol(client, lib, primary_cell, run_dir, debug=debug)

    # Step 3: Extract pin info from redistributed symbol
    pins = extract_dut_pins(client, lib, primary_cell)

    for pin in pins:
        if pin.side not in ("left", "right"):
            print(f"[step3] WARNING: pin {pin.name} side={pin.side!r}, correcting to 'left'")
            pin.side = "left"

    # Write pin_info.json — symbol export ends here
    write_pin_info_json(pins, lib, primary_cell, vdd_value, run_dir / "pin_info.json")

    result = DutContext(
        lib=lib,
        primary_cell=primary_cell,
        pins=pins,
        run_dir=run_dir,
        vdd_value=vdd_value,
        symbol_exported=symbol_ok,
        redistributed=redistributed,
    )
    result.save(run_dir)

    print(f"\n{'='*60}")
    print(f" Symbol Export Complete")
    print(f"  pin_info.json → {run_dir / 'pin_info.json'}")
    print(f"")
    print(f"  Next: read references/pin_classification.md + pin_info.json,")
    print(f"        classify every pin, write pin_classifications.json")
    print(f"        to {run_dir}")
    print(f"  Then: python scripts/tb_builder.py")
    print(f"{'='*60}\n")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SMIC180 IO Ring Simulator symbol export - symbol generation, redistribution, pin extraction"
    )
    parser.add_argument("lib", help="Virtuoso library name")
    parser.add_argument("cell", help="Primary cell name (must have schematic view)")
    parser.add_argument("--vdd", type=float, default=1.8, metavar="V",
                        help="VDD supply voltage in volts (default: 1.8)")
    parser.add_argument("--debug", action="store_true",
                        help="Write debug JSON files (layout_result.json)")
    args = parser.parse_args()

    try:
        run_symbol_export(args.lib, args.cell, vdd_value=args.vdd, debug=args.debug)
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

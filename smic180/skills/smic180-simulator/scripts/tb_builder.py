#!/usr/bin/env python3
"""TB Builder: create testbench cellview, place DUT, wire labels, place sources/loads.

Steps 4a-d:
  4a. Create {primary_cell}_tb schematic cellview
  4b. Place DUT symbol instance
  4c. Add wire labels (label-based wiring - no explicit wires drawn)
  4d. Place sources, loads, PVSS, GND_REF; set CDF parameters

Reads pin_classifications.json written by the LLM after symbol_export.
Falls back to heuristic classification if the file is absent (warning only).

Usage:
    python scripts/tb_builder.py                       # uses .latest_run
    python scripts/tb_builder.py --run-dir <path>      # explicit run dir
    python scripts/tb_builder.py --cleanup              # delete _tb cellview from failed run

Exit codes:
    0  success
    1  error (printed to stderr)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

_SIM_IO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SIM_IO))

from sim_io.flow import (
    DutContext,
    SimFlowResult,
    load_dut_context,
    load_llm_result,
    classify_pin,
    create_tb_cellview,
    place_dut,
    add_wire_labels,
    place_sources_and_loads,
)
from sim_io.config import read_latest_run
from sim_io.library_mapping import require_configured_library_mapping
from sim_io.pin_types import ClassificationResult, PinClassification, PinInfo


def _cleanup_tb(client: VirtuosoClient, lib: str, tb_cell: str) -> None:
    """Delete the _tb cellview from Virtuoso (used after failed runs)."""
    r = client.execute_skill(f'ddDeleteCellView("{lib}" "{tb_cell}" "schematic")')
    if r.errors:
        print(f"[cleanup] Failed to delete {lib}/{tb_cell}/schematic: {r.errors}")
    else:
        print(f"[cleanup] Deleted {lib}/{tb_cell}/schematic")

    r2 = client.execute_skill(f'ddDeleteCellView("{lib}" "{tb_cell}" "maestro")')
    if not r2.errors:
        print(f"[cleanup] Deleted {lib}/{tb_cell}/maestro")


def run_tb_builder(
    dut_context: DutContext,
    *,
    client: VirtuosoClient | None = None,
) -> tuple[list[str], list[str], dict[str, PinClassification], ClassificationResult | None]:
    """TB Builder: create testbench cellview, place DUT, wire labels, place sources/loads.

    Returns (labels, sources, classifications, classif_result).

    On failure, attempts to clean up the partially-created _tb cellview.
    """
    if client is None:
        client = VirtuosoClient.from_env()

    lib = dut_context.lib
    primary_cell = dut_context.primary_cell
    tb_cell = dut_context.tb_cell
    pins = dut_context.pins
    vdd_value = dut_context.vdd_value
    require_configured_library_mapping(client, lib)

    # Load LLM classifications from run_dir/pin_classifications.json
    classif_result: ClassificationResult | None = load_llm_result(
        dut_context.run_dir, cell=primary_cell
    )
    from sim_io.pin_types import build_classification_map
    classifications = build_classification_map(classif_result) if classif_result else {}

    print(f"\n{'='*60}")
    print(f" TB Builder: {lib}/{tb_cell}  (LLM={bool(classifications)})")
    print(f"{'='*60}\n")

    try:
        # Step 4a: Create _tb cellview
        create_tb_cellview(client, lib, primary_cell)

        # Step 4b: Place DUT instance
        place_dut(client, lib, tb_cell, primary_cell)

        # Step 4c: Add wire labels on DUT pins
        labels = add_wire_labels(client, lib, tb_cell, pins, result=classif_result) if pins else []

        # Step 4d: Place sources & loads based on LLM classification
        sources = place_sources_and_loads(
            lib, tb_cell, pins,
            classifications=classifications,
            result=classif_result,
            vdd_value=vdd_value,
            client=client,
        ) if pins else []
    except Exception:
        print(f"[tb_builder] FAILED - cleaning up {lib}/{tb_cell}")
        try:
            _cleanup_tb(client, lib, tb_cell)
        except Exception as cleanup_exc:
            print(f"[tb_builder] Cleanup also failed: {cleanup_exc}")
        raise

    print(f"\n{'='*60}")
    print(f" TB Builder Complete")
    print(f"  TB cellview:       {lib}/{tb_cell}/schematic")
    print(f"  DUT labels added:  {len(labels)}")
    print(f"  Sources placed:    {len(sources)}")
    print(f"  LLM classified:    {bool(classifications)}")
    types: dict[str, int] = {}
    for p in pins:
        t = classify_pin(p, classifications)
        types[t] = types.get(t, 0) + 1
    print(f"  Pin types:         {dict(sorted(types.items()))}")
    print(f"  Next: python scripts/spectre_runner.py")
    print(f"{'='*60}\n")

    return labels, sources, classifications, classif_result


def _resolve_run_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return read_latest_run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SMIC180 IO Ring Simulator TB builder - create testbench, place DUT, wire, sources/loads"
    )
    parser.add_argument("--run-dir", metavar="PATH",
                        help="Run directory from symbol_export (default: reads .latest_run)")
    parser.add_argument("--cleanup", action="store_true",
                        help="Delete _tb cellview from a failed run")
    args = parser.parse_args()

    try:
        run_dir = _resolve_run_dir(args.run_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        dut_context = load_dut_context(run_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}; run symbol_export first.", file=sys.stderr)
        sys.exit(1)

    # --cleanup: delete _tb cellview and exit
    if args.cleanup:
        client = VirtuosoClient.from_env()
        _cleanup_tb(client, dut_context.lib, dut_context.tb_cell)
        print("Cleanup done.")
        sys.exit(0)

    classif_json = run_dir / "pin_classifications.json"
    if not classif_json.exists():
        print(f"WARNING: {classif_json} not found - falling back to heuristic classification.",
              file=sys.stderr)

    try:
        run_tb_builder(dut_context)
        print(f"\nTB builder complete.")
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

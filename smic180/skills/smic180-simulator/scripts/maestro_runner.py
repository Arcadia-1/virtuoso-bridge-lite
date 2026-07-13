#!/usr/bin/env python3
"""Maestro Runner: Maestro test setup + optional simulation.

Steps 4e-5:
  4e. Maestro test setup (configures cellview for GUI use too)
   5. Maestro simulation (if --run-sim)
      -> measurements.json, maestro_detail.csv, plots/

Reads pin_classifications.json written by the LLM after symbol_export.
Falls back to heuristic classification if the file is absent (warning only).

Usage:
    python scripts/maestro_runner.py                       # Maestro setup only
    python scripts/maestro_runner.py --run-sim             # setup + run simulation
    python scripts/maestro_runner.py --run-dir <path>      # explicit run dir
    python scripts/maestro_runner.py --intent "DC sweep VDD 0->1.8"

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
    load_dut_context,
    load_llm_result,
    classify_pin,
)
from sim_io.config import read_latest_run
from sim_io.pin_types import ClassificationResult, PinClassification, PinInfo


def run_maestro_runner(
    dut_context: DutContext,
    *,
    classifications: dict[str, PinClassification] | None = None,
    classif_result: ClassificationResult | None = None,
    run_sim: bool = False,
    client: VirtuosoClient | None = None,
    user_intent: str = "",
) -> tuple[bool | None, str | None, list[Path]]:
    """Maestro Runner: Maestro test setup + optional simulation.

    Returns (sim_run_ok, sim_verdict, plot_paths).
    """
    if client is None:
        client = VirtuosoClient.from_env()

    lib = dut_context.lib
    tb_cell = dut_context.tb_cell
    pins = dut_context.pins
    run_dir = dut_context.run_dir
    vdd_value = dut_context.vdd_value

    # Re-derive classifications if not provided
    if classifications is None:
        classif_result = classif_result or load_llm_result(run_dir, cell=dut_context.primary_cell)
        from sim_io.pin_types import build_classification_map
        classifications = build_classification_map(classif_result) if classif_result else {}

    print(f"\n{'='*60}")
    print(f" Maestro Runner: {lib}/{tb_cell}")
    print(f"{'='*60}\n")

    # Step 4e: Maestro setup
    from sim_io.maestro import build_maestro_setup
    from sim_io.site_config import SiteConfig
    from sim_io.sim.config import resolve_sim_config, sim_config_from_site

    site = SiteConfig.from_env()
    deck_config = resolve_sim_config(
        run_dir=run_dir, lib=lib, cell=tb_cell,
        vdd_value=vdd_value, user_intent=user_intent,
    )
    if not deck_config.model_includes:
        deck_config.model_includes = sim_config_from_site(
            vdd_value=vdd_value
        ).model_includes
        print(f"[sim-config] Injected {len(deck_config.model_includes)} model includes from site config")
    try:
        build_maestro_setup(client, lib, tb_cell, deck_config, pins=pins,
                            auto_close=True, classifications=classifications)
        print("[step4e] Maestro setup saved")
    except Exception as exc:
        print(f"[step4e] WARNING: Maestro setup failed: {exc}")

    # Step 5: Maestro simulation (optional)
    sim_run_ok = None
    sim_verdict = None
    plot_paths: list[Path] = []

    if run_sim and pins:
        from sim_io.maestro import run_maestro_sim, parse_maestro_measurements, plot_maestro_waves

        wave_signals = [
            f"/{p.name}" for p in pins
            if classify_pin(p, classifications) not in ("ground", "no_connect")
        ]

        mae_result = run_maestro_sim(
            client, lib, tb_cell,
            test_name=f"{tb_cell}_test",
            timeout=600,
            export_waves=True,
            wave_signals=wave_signals,
            run_dir=run_dir,
        )
        sim_run_ok = mae_result.sim_ok

        if mae_result.sim_ok:
            measurements = parse_maestro_measurements(
                mae_result, pins,
                classifications=classifications,
                vdd=vdd_value,
            )
            (run_dir / "measurements.json").write_text(
                json.dumps(measurements, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"[step5] measurements.json written "
                  f"({measurements.get('num_pins_measured', 0)}/{measurements.get('num_pins_total', 0)} pins)")

            plot_paths = plot_maestro_waves(
                run_dir / "maestro_waves",
                run_dir / "plots",
            )

    print(f"\n{'='*60}")
    print(f" Maestro Runner Complete")
    print(f"  Output dir:        {run_dir}")
    if sim_run_ok is not None:
        print(f"  Sim run:           {'OK' if sim_run_ok else 'FAILED'} (maestro)")
    if plot_paths:
        print(f"  SVG plots:         {len(plot_paths)} file(s) in {run_dir / 'plots'}")
    print(f"{'='*60}\n")

    return sim_run_ok, None, plot_paths


def _resolve_run_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return read_latest_run()


def _load_classifications(run_dir: Path, cell: str):
    """Load LLM classifications, returning (classifications_dict, classif_result_or_None)."""
    classif_result = load_llm_result(run_dir, cell=cell)
    from sim_io.pin_types import build_classification_map
    classifications = build_classification_map(classif_result) if classif_result else {}
    return classifications, classif_result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SMIC180 IO Ring Simulator Maestro runner - Maestro setup + optional simulation"
    )
    parser.add_argument("--run-dir", metavar="PATH",
                        help="Run directory from symbol_export (default: reads .latest_run)")
    parser.add_argument("--run-sim", action="store_true",
                        help="Run Maestro simulation after setup")
    parser.add_argument("--intent", default="", metavar="TEXT",
                        help="Free-text simulation intent for deck configuration")
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

    classif_json = run_dir / "pin_classifications.json"
    if not classif_json.exists():
        print(f"WARNING: {classif_json} not found - falling back to heuristic classification.",
              file=sys.stderr)

    try:
        classifications, classif_result = _load_classifications(run_dir, dut_context.primary_cell)
        client = VirtuosoClient.from_env()

        sim_ok, sim_verdict, _ = run_maestro_runner(
            dut_context,
            classifications=classifications,
            classif_result=classif_result,
            run_sim=args.run_sim,
            client=client,
            user_intent=args.intent,
        )
        print(f"\nMaestro runner complete.")
        if args.run_sim and sim_ok is not None:
            status = "OK" if sim_ok else "FAILED"
            print(f"  Simulation  : {status} (maestro)")
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

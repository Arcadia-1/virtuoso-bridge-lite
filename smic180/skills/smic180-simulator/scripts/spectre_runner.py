#!/usr/bin/env python3
"""Direct Spectre runner with Maestro sync.

This is the default SIM-IO simulation route:

1. Load dut_context.json and sim_config.json from a run directory.
2. Export a fresh Spectre netlist from the generated testbench.
3. Build deck.scs from the resolved simulation configuration.
4. Run Spectre directly through SpectreSimulator.
5. Parse PSF results locally.
6. Sync the same resolved simulation settings into Maestro for GUI use.

Maestro is configured and saved, but Maestro simulation is not run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

_SIM_IO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SIM_IO))

from sim_io.flow import load_dut_context, load_llm_result
from sim_io.config import read_latest_run
from sim_io.pin_types import build_classification_map
from sim_io.site_config import SiteConfig
from sim_io.sim.config import resolve_sim_config, sim_config_from_site
from sim_io.sim.run import run_sim_run
from sim_io.maestro import build_maestro_setup


def _resolve_run_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return read_latest_run()


def run_spectre_with_maestro_sync(
    run_dir: Path,
    *,
    user_intent: str = "",
    spectre_mode: str = "lx",
    spectre_timeout: int = 600,
    sync_maestro: bool = True,
    open_gui: bool = False,
    client: VirtuosoClient | None = None,
    corners: list[str] | None = None,
) -> int:
    """Run direct Spectre and sync Maestro setup without running Maestro sim.

    When ``corners`` is provided, runs all specified process corners in parallel
    via bridge-lite's SpectreSimulator.run_parallel() and writes
    ``corner_results.json`` to run_dir.  The single (tt) Spectre run is
    always executed first as the baseline.
    """
    if os.name == "nt":
        os.environ.setdefault("VB_DISABLE_CONTROL_MASTER", "1")

    dut_context = load_dut_context(run_dir)
    if client is None:
        client = VirtuosoClient.from_env()
    site = SiteConfig.from_env()

    classif_result = load_llm_result(run_dir, cell=dut_context.primary_cell)
    classifications = build_classification_map(classif_result) if classif_result else {}

    deck_config = resolve_sim_config(
        run_dir=run_dir,
        lib=dut_context.lib,
        cell=dut_context.tb_cell,
        vdd_value=dut_context.vdd_value,
        user_intent=user_intent,
    )
    if not deck_config.model_includes:
        deck_config.model_includes = sim_config_from_site(
            vdd_value=dut_context.vdd_value
        ).model_includes
        print(f"[sim-config] Injected {len(deck_config.model_includes)} model includes from site config")

    print(f"\n{'=' * 60}")
    print(f" Direct Spectre: {dut_context.lib}/{dut_context.tb_cell}")
    print(f" Run dir:        {run_dir}")
    print(f" Maestro sync:   {'enabled' if sync_maestro else 'disabled'}")
    if corners:
        print(f" Corners:        {', '.join(corners)}")
    print(f"{'=' * 60}\n")

    sim_result = run_sim_run(
        dut_context.lib,
        dut_context.tb_cell,
        dut_context.pins,
        run_dir,
        deck_config=deck_config,
        site=site,
        client=client,
        spectre_mode=spectre_mode,
        spectre_timeout=spectre_timeout,
        user_intent=user_intent,
        vdd_value=dut_context.vdd_value,
    )

    # Multi-corner parallel run (if requested and baseline succeeded)
    if corners and sim_result.spectre_ok:
        from sim_io.sim.corner import run_corners_parallel

        # Use the raw si-exported netlist (circuit only), NOT the full deck.
        # deck.scs already contains the header/separator/deck config, so
        # passing it to build_corner_deck would cause duplicate content (SFE-59).
        corner_netlist_path = Path(sim_result.netlist_path) if sim_result.netlist_path else None
        if corner_netlist_path is None or not corner_netlist_path.exists():
            print("[corner] WARNING: Raw netlist not found; skipping corner sweep")
        else:
            print(f"\n{"=" * 60}")
            print(f" Multi-Corner Sweep: {", ".join(corners)}")
            print(f"{"=" * 60}\n")

            multi = run_corners_parallel(
                netlist_path=corner_netlist_path,
                base_deck=deck_config,
                run_dir=run_dir,
                corners=corners,
                spectre_mode=spectre_mode,
                spectre_timeout=spectre_timeout,
            )
            multi.save(run_dir)

    maestro_ok = None
    maestro_error = ""
    if sync_maestro:
        if not sim_result.deck_path:
            maestro_ok = False
            maestro_error = "Skipped: direct Spectre route did not build deck.scs"
            print(f"[maestro-sync] {maestro_error}")
        else:
            try:
                build_maestro_setup(
                    client,
                    dut_context.lib,
                    dut_context.tb_cell,
                    deck_config,
                    pins=dut_context.pins,
                    auto_close=True,
                    classifications=classifications,
                )
                maestro_ok = True
                print("[maestro-sync] Maestro setup saved; Maestro simulation was not run.")
            except Exception as exc:
                maestro_ok = False
                maestro_error = str(exc)
                print(f"[maestro-sync] WARNING: Maestro sync failed: {exc}")

        sync_meta = {
            "lib": dut_context.lib,
            "tb_cell": dut_context.tb_cell,
            "synced": maestro_ok,
            "error": maestro_error,
            "simulation_source": "direct_spectre",
            "maestro_simulation_run": False,
            "spectre_ok": sim_result.spectre_ok,
            "deck_path": sim_result.deck_path,
        }
        if corners:
            sync_meta["corners_requested"] = corners
        result_path = run_dir / "sim_run_result.json"
        if result_path.exists():
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
            result_data["maestro_sync"] = sync_meta
            result_path.write_text(
                json.dumps(result_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    # Open Maestro GUI if requested
    if open_gui:
        try:
            from virtuoso_bridge.virtuoso.maestro import open_gui_session
            session = open_gui_session(
                client,
                dut_context.lib,
                dut_context.tb_cell,
                timeout=60,
            )
            print(f"[gui] Maestro (ADE Assembler) opened: {session}")
        except Exception as exc:
            print(f"[gui] WARNING: Failed to open Maestro GUI: {exc}")

    return 0 if sim_result.spectre_ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SMIC180 IO Ring Simulator: direct Spectre + optional multi-corner sweep + Maestro sync"
    )
    parser.add_argument("--run-dir", metavar="PATH",
                        help="Run directory from symbol_export (default: reads .latest_run)")
    parser.add_argument("--intent", default="", metavar="TEXT",
                        help="Free-text simulation intent for config fallback")
    parser.add_argument("--spectre-mode", default="lx",
                        help="Spectre execution mode/preset (default: lx)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Spectre timeout in seconds (default: 600)")
    parser.add_argument("--no-maestro-sync", action="store_true",
                        help="Run direct Spectre only; do not sync Maestro setup")
    parser.add_argument("--no-open-gui", action="store_true",
                        help="Do not open Maestro (ADE Assembler) GUI after Spectre run")
    parser.add_argument("--corners", nargs="*", default=None,
                        metavar="CORNER",
                        help="Process corners for parallel sweep, e.g. tt ff ss fnsp snfp. "
                             "Default: all 5 SMIC180 corners. "
                             "Pass without values for all corners, or list specific ones.")
    args = parser.parse_args()

    # --corners with no values -> all SMIC180 corners
    # --corners tt ff -> specific corners
    # (no --corners) -> None (no sweep)
    requested_corners = args.corners
    if requested_corners is not None and len(requested_corners) == 0:
        from sim_io.sim.corner import SMIC180_CORNERS
        requested_corners = SMIC180_CORNERS

    try:
        resolved_run_dir = _resolve_run_dir(args.run_dir)
        code = run_spectre_with_maestro_sync(
            resolved_run_dir,
            user_intent=args.intent,
            spectre_mode=args.spectre_mode,
            spectre_timeout=args.timeout,
            sync_maestro=not args.no_maestro_sync,
            open_gui=not args.no_open_gui,
            corners=requested_corners,
        )
        sys.exit(code)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

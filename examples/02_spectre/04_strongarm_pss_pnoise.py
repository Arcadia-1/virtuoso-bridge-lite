#!/usr/bin/env python3
"""Run a single-point StrongArm comparator PSS + Pnoise simulation."""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _result_io import print_result_counts, print_timing_summary, save_summary_json
from assets.strongarm_cmp.analyze_strongarm_pss_pnoise import (
    extract_metrics,
    write_time_domain_plot,
)
from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

ROOT = Path(__file__).resolve().parent
ASSET_DIR = ROOT / "assets" / "strongarm_cmp"
BASE_NETLIST = ASSET_DIR / "tb_cmp_strongarm.scs"
OUT_DIR = ROOT / "output" / "strongarm_pss_pnoise"
RUN_NETLIST = OUT_DIR / "tb_cmp_strongarm_run.scs"
METRICS_JSON = OUT_DIR / "strongarm_pss_pnoise_metrics.json"
SUMMARY_JSON = OUT_DIR / "strongarm_pss_pnoise_result.json"
PLOT_PATH = OUT_DIR / "strongarm_pss_time_domain.png"
DEFAULT_VCM = 0.45


def _parse_args(argv: list[str]) -> bool:
    allowed = {"--analyze-only"}
    unknown = [arg for arg in argv if arg not in allowed]
    if unknown:
        raise SystemExit(f"Unsupported arguments: {' '.join(unknown)}")
    return "--analyze-only" in argv


def _build_netlist(base_text: str, vcm: float) -> str:
    lines_out: list[str] = []
    for line in base_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("parameters ") and "Vcm=" in stripped:
            parts = stripped.split()
            lines_out.append(
                " ".join(f"Vcm={vcm:.4f}" if part.startswith("Vcm=") else part for part in parts)
            )
        else:
            lines_out.append(line)
    return "\n".join(lines_out) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    analyze_only = _parse_args(argv)
    vcm = DEFAULT_VCM

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    if not BASE_NETLIST.exists():
        print(f"Base netlist not found: {BASE_NETLIST}")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_dir = OUT_DIR / f"{RUN_NETLIST.stem}.raw"

    print(f"Vcm : {vcm:.3f} V")
    if analyze_only:
        print("[Mode] Analyze existing PSS/Pnoise results only")
        if not raw_dir.exists():
            print(f"Raw result directory not found: {raw_dir}")
            return 1
        result = None
    else:
        RUN_NETLIST.write_text(
            _build_netlist(BASE_NETLIST.read_text(encoding="utf-8"), vcm),
            encoding="utf-8",
        )

        print("[Run] Running StrongArm single-point PSS + Pnoise remotely ...")
        sim = SpectreSimulator.from_env(
            spectre_cmd=os.getenv("SPECTRE_CMD", "spectre"),
            spectre_args=spectre_mode_args("ax"),
            work_dir=OUT_DIR,
            output_format="psfascii",
        )
        result = sim.run_simulation(RUN_NETLIST, {})

        print(f"Status : {result.status.value}")
        print_result_counts(result)
        if result.errors:
            print("Errors :")
            for error in result.errors[:8]:
                print(f"  {error}")
        if not result.ok:
            return 1

    metrics = extract_metrics(raw_dir, vcm=vcm)
    METRICS_JSON.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    plot_path = write_time_domain_plot(raw_dir, PLOT_PATH)

    print("\nMetrics:")
    for key, value in metrics.items():
        print(f"  {key}: {value}")

    if result is not None:
        save_summary_json(
            result,
            SUMMARY_JSON,
            extra={
                "vcm": vcm,
                "netlist": str(RUN_NETLIST),
                "metrics_json": str(METRICS_JSON),
                "plot_path": str(plot_path),
                "analyze_only": analyze_only,
            },
        )
    else:
        SUMMARY_JSON.write_text(
            json.dumps(
                {
                    "status": "analysis_only",
                    "ok": True,
                    "vcm": vcm,
                    "metrics_json": str(METRICS_JSON),
                    "plot_path": str(plot_path),
                    "raw_dir": str(raw_dir),
                    "analyze_only": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    print(f"\n[Metrics] {METRICS_JSON}")
    print(f"[Plot] {plot_path}")
    print(f"[Summary] {SUMMARY_JSON}")
    if result is not None:
        print_timing_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run parallel Spectre simulations sweeping inverter load capacitance.

Demonstrates the parallel execution API:

- ``SpectreSimulator.set_max_workers()``
- ``SpectreSimulator.run_parallel()``
- ``SpectreSimulator.wait_all()`` (called internally by ``run_parallel``)
- ``SpectreSimulator.submit()`` (direct submit mode)
- ``SpectreSimulator.shutdown()``

Each simulation modifies the load capacitance (Cload) in the inverter
netlist and runs remotely.  After all simulations complete, propagation
delay vs. load capacitance is plotted.

Usage::

    python examples/02_spectre/05_parallel_sweep.py
    python examples/02_spectre/05_parallel_sweep.py --workers 4
    python examples/02_spectre/05_parallel_sweep.py --mode ax
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

matplotlib.use("Agg")

from virtuoso_bridge.models import SimulationResult
from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

ROOT = Path(__file__).resolve().parent
BASE_NETLIST = ROOT / "assets" / "inv_tb" / "spectre_inv_tb.scs"
OUT_DIR = ROOT / "output" / "parallel_sweep"
PLOT_PATH = OUT_DIR / "sweep_delay_vs_cload.png"

CLOAD_VALUES = [5e-15, 10e-15, 20e-15, 50e-15, 100e-15]
SUPPORTED_MODES = ("spectre", "aps", "x", "cx", "ax", "mx", "lx", "vx")


def _parse_mode(argv: list[str]) -> str:
    mode = "ax"
    if "--mode" in argv:
        idx = argv.index("--mode")
        if idx + 1 >= len(argv):
            raise SystemExit("--mode requires one of: spectre, aps, x, cx, ax, mx, lx, vx")
        mode = argv[idx + 1].strip().lower()
    if mode not in SUPPORTED_MODES:
        raise SystemExit(f"Unsupported mode '{mode}'. Use: spectre, aps, x, cx, ax, mx, lx, vx")
    return mode


def _parse_workers(argv: list[str]) -> int:
    if "--workers" in argv:
        idx = argv.index("--workers")
        if idx + 1 >= len(argv):
            raise SystemExit("--workers requires an integer")
        return int(argv[idx + 1])
    return 4


def _build_sweep_netlist(base_text: str, cload: float) -> str:
    cload_str = f"{cload * 1e15:.1f}f"
    return re.sub(
        r"(Cload\s+\(VOUT\s+0\)\s+capacitor\s+c=)\S+",
        rf"\g<1>{cload_str}",
        base_text,
    )


def _measure_prop_delay(
    time: list[float],
    vin: list[float],
    vout: list[float],
) -> float | None:
    """Measure 50%-50% inverting propagation delay (VIN rising edge)."""
    if not time or not vin or not vout:
        return None

    vdd = max(vin)
    vdd_half = vdd / 2.0

    t_vin = None
    for i in range(1, len(time)):
        if vin[i - 1] <= vdd_half < vin[i]:
            t_vin = time[i - 1] + (time[i] - time[i - 1]) * (
                vdd_half - vin[i - 1]
            ) / (vin[i] - vin[i - 1])
            break

    t_vout = None
    for i in range(1, len(time)):
        if vout[i - 1] >= vdd_half > vout[i]:
            t_vout = time[i - 1] + (time[i] - time[i - 1]) * (
                vdd_half - vout[i - 1]
            ) / (vout[i] - vout[i - 1])
            break

    if t_vin is not None and t_vout is not None:
        return abs(t_vout - t_vin)
    return None


def _write_sweep_plot(
    cloads_fF: list[float],
    delays_ps: list[float],
    out_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(8, 5), dpi=160)
    ax.plot(cloads_fF, delays_ps, "o-", linewidth=2, markersize=8, color="#1f77b4")
    ax.set_xlabel("Load Capacitance (fF)")
    ax.set_ylabel("Propagation Delay (ps)")
    ax.set_title("Inverter Delay vs Load Capacitance (Parallel Sweep)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"[Plot] {out_path}")


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    mode = _parse_mode(argv)
    max_workers = _parse_workers(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if not BASE_NETLIST.exists():
        print(f"Netlist not found: {BASE_NETLIST}")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base_text = BASE_NETLIST.read_text(encoding="utf-8")

    sweep_dir = OUT_DIR / "sweep_netlists"
    sweep_dir.mkdir(parents=True, exist_ok=True)

    tasks: list[tuple[Path, dict]] = []
    for cload in CLOAD_VALUES:
        netlist_text = _build_sweep_netlist(base_text, cload)
        netlist_path = sweep_dir / f"inv_cload_{cload * 1e15:.1f}f.scs"
        netlist_path.write_text(netlist_text, encoding="utf-8")
        tasks.append((netlist_path, {}))

    print(f"[Sweep] Cload values: {[f'{c * 1e15:.1f}fF' for c in CLOAD_VALUES]}")
    print(f"[Parallel] {len(tasks)} simulations, {max_workers} workers, mode '{mode}'")

    # --- Run all sweep points in parallel via SpectreSimulator's pool ----
    # One SpectreSimulator instance manages the worker pool. Each task gets
    # its own ``<netlist.stem>__<run-id>`` local work directory and a remote
    # uuid-based directory automatically, so PSF and auxiliary files cannot
    # collide even when the same netlist is submitted more than once.
    sim = SpectreSimulator.from_env(
        spectre_cmd=os.getenv("SPECTRE_CMD", "spectre"),
        spectre_args=spectre_mode_args(mode),
        work_dir=OUT_DIR,
        output_format="psfascii",
    )
    sim.set_max_workers(max_workers)

    # ``run_parallel`` is the one-shot convenience wrapper -- submit a list
    # of (netlist, params) tasks, get a list of SimulationResult back in
    # the same order.  Equivalent low-level form:
    #     futures = [sim.submit(n, p) for n, p in tasks]
    #     results = SpectreSimulator.wait_all(futures)
    try:
        results: list[SimulationResult] = sim.run_parallel(tasks)
    finally:
        sim.shutdown()

    # --- Analyze results -------------------------------------------------
    cloads_fF: list[float] = []
    delays_ps: list[float] = []

    print(f"\n{'Cload':>8s}  {'Status':>8s}  {'Delay (ps)':>10s}  {'Signals':>8s}")
    print("-" * 44)

    for cload, result in zip(CLOAD_VALUES, results):
        c_fF = cload * 1e15
        status = "OK" if result.ok else "FAIL"

        if result.ok:
            time_vals = result.data.get("time", [])
            vin = result.data.get("VIN", [])
            vout = result.data.get("VOUT", [])
            delay = _measure_prop_delay(time_vals, vin, vout)
            delay_str = f"{delay * 1e12:.2f}" if delay is not None else "N/A"
            n_sig = len(result.data)

            if delay is not None:
                cloads_fF.append(c_fF)
                delays_ps.append(delay * 1e12)
        else:
            delay_str = "N/A"
            n_sig = 0
            if result.errors:
                print(f"  Error: {result.errors[0]}")

        print(f"{c_fF:7.1f}fF  {status:>8s}  {delay_str:>10s}  {n_sig:>8d}")

    if cloads_fF and delays_ps:
        _write_sweep_plot(cloads_fF, delays_ps, PLOT_PATH)

    summary = {
        "mode": mode,
        "max_workers": max_workers,
        "cload_values_fF": [c * 1e15 for c in CLOAD_VALUES],
        "succeeded": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "delays_ps": dict(zip(
            [f"{c:.1f}fF" for c in cloads_fF],
            delays_ps,
        )),
    }
    summary_path = OUT_DIR / "sweep_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n[Summary] {summary_path}")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

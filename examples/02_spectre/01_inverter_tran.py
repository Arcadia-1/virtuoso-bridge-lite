#!/usr/bin/env python3
"""Run a remote Spectre transient simulation of an inverter.

This example is intentionally remote-only. Spectre is expected to run on the
Virtuoso server reached via SSH using ``VB_REMOTE_HOST`` / ``VB_REMOTE_USER``
from ``.env``.

Prerequisites:
  - ``.env`` with remote SSH settings
  - Spectre available on the remote host PATH, or set ``SPECTRE_CMD``
  - ``VB_PDK_SPECTRE_INCLUDE`` in ``.env`` pointing to the PDK spectre model file

Usage::

    python examples/02_spectre/01_inverter_tran.py
    python examples/02_spectre/01_inverter_tran.py --mode spectre
    python examples/02_spectre/01_inverter_tran.py --mode aps
    python examples/02_spectre/01_inverter_tran.py --mode cx
    python examples/02_spectre/01_inverter_tran.py --mode ax
    python examples/02_spectre/01_inverter_tran.py --mode mx
    python examples/02_spectre/01_inverter_tran.py --mode lx
    python examples/02_spectre/01_inverter_tran.py --mode vx

The netlist simulates a TSMC 28nm CMOS inverter driven by a 1 GHz pulse.
After a successful run it prints the first few time points and VOUT values.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WORK_DIR = Path(__file__).resolve().parent / "output" / "spectre_inv"

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

from _result_io import print_result_counts, print_timing_summary, save_summary_json, save_waveforms_csv
from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

matplotlib.use("Agg")

NETLIST = Path(__file__).resolve().parent / "assets" / "spectre_inv_tb.scs"
RUN_NETLIST = WORK_DIR / "spectre_inv_tb_run.scs"
PLOT_PATH = WORK_DIR / "inv_waveforms.png"
CSV_PATH = WORK_DIR / "inv_waveforms.csv"
SUMMARY_PATH = WORK_DIR / "inv_result.json"
SUPPORTED_MODES = ("spectre", "aps", "x", "cx", "ax", "mx", "lx", "vx")


def _write_waveform_plot(
    out_path: Path,
    time_values: list[float],
    vin_values: list[float],
    vout_values: list[float],
) -> None:
    """Write VIN/VOUT waveforms to a stacked PNG figure."""
    time_ns = np.asarray(time_values, dtype=float) * 1e9
    vin = np.asarray(vin_values, dtype=float)
    vout = np.asarray(vout_values, dtype=float)

    fig, (ax_in, ax_out) = plt.subplots(
        2,
        1,
        figsize=(10, 6.4),
        dpi=160,
        sharex=True,
    )
    fig.suptitle("Spectre Inverter Transient")

    ax_in.plot(time_ns, vin, color="#1f77b4", linewidth=2.2)
    ax_in.set_ylabel("VIN (V)")
    ax_in.grid(True, color="#d9d9d9", linewidth=0.8)

    ax_out.plot(time_ns, vout, color="#d62728", linewidth=2.2)
    ax_out.set_xlabel("Time (ns)")
    ax_out.set_ylabel("VOUT (V)")
    ax_out.grid(True, color="#d9d9d9", linewidth=0.8)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def _parse_mode(argv: list[str]) -> str:
    mode = "ax"
    if "--mode" in argv:
        idx = argv.index("--mode")
        if idx + 1 >= len(argv):
            raise SystemExit("--mode requires one of: spectre, aps, x, cx, ax, mx, lx, vx")
        mode = argv[idx + 1].strip().lower()
    elif argv:
        mode = argv[0].strip().lower()
    if mode not in SUPPORTED_MODES:
        raise SystemExit(f"Unsupported mode '{mode}'. Use: spectre, aps, x, cx, ax, mx, lx, vx")
    return mode
def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    mode = _parse_mode(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    if not NETLIST.exists():
        print(f"Netlist not found: {NETLIST}")
        return 1

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    spectre_cmd = os.getenv("SPECTRE_CMD", "spectre")
    print(f"[Run] Running Spectre remotely in mode '{mode}' (reads VB_REMOTE_HOST from .env) ...")
    sim = SpectreSimulator.from_env(
        spectre_cmd=spectre_cmd,
        spectre_args=spectre_mode_args(mode),
        work_dir=WORK_DIR,
        output_format="psfascii",
    )

    result = sim.run_simulation(NETLIST, {})

    print(f"[Status] {result.status.value}")
    print_result_counts(result)
    if result.errors:
        print("[Error Details]")
        for e in result.errors[:5]:
            print(f"  {e}")

    if not result.ok:
        return 1

    signals = list(result.data.keys())
    print(f"[Signals] {signals}")

    time = result.data.get("time", [])
    vout = result.data.get("VOUT", [])
    if time and vout:
        print(f"\n[Preview] First 5 time points (s) and VOUT (V):")
        for t, v in zip(time[:5], vout[:5]):
            print(f"  t={t:.3e}  VOUT={v:.4f}")

    vin = result.data.get("VIN", [])
    if time and vin and vout and len(time) == len(vin) == len(vout):
        _write_waveform_plot(PLOT_PATH, time, vin, vout)
        print(f"[Plot] {PLOT_PATH}")
        save_waveforms_csv(result.data, CSV_PATH)
        print(f"[CSV] {CSV_PATH}")

    save_summary_json(
        result,
        SUMMARY_PATH,
        extra={
            "mode": mode,
            "netlist": str(NETLIST),
        },
    )
    print(f"\n[Summary] {SUMMARY_PATH}")
    print_timing_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

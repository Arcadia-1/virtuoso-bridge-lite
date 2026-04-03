#!/usr/bin/env python3
"""Run DC + AC simulation on an RC filter and plot frequency response.

Circuit: VDD → R0 (1K) → VO → C1 (50f) → GND
         VDD → C0 (50f) → GND (reference cap)

Expected f_3dB = 1 / (2*pi*R*C) = 1 / (2*pi*1e3*50e-15) ≈ 3.18 GHz

Usage::

    python examples/02_spectre/02_cap_dc_ac.py
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _result_io import print_result_counts, print_timing_summary, save_summary_json

ROOT = Path(__file__).resolve().parent
NETLIST = ROOT / "assets" / "cap_dc_ac" / "tb_cap_dc_ac.scs"
OUT_DIR = ROOT / "output" / "cap_dc_ac"


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sim = SpectreSimulator.from_env(
        spectre_cmd=os.getenv("SPECTRE_CMD", "spectre"),
        spectre_args=spectre_mode_args("ax"),
        work_dir=OUT_DIR,
        output_format="psfascii",
    )

    print("[Run] RC filter DC + AC simulation ...")
    result = sim.run_simulation(NETLIST, {})

    print(f"Status : {result.status.value}")
    print_result_counts(result)
    if result.errors:
        for e in result.errors[:5]:
            print(f"  {e}")
    if not result.ok:
        return 1

    # DC operating point
    dc_vdd = result.data.get("dc_VDD")
    dc_vo = result.data.get("dc_VO")
    if dc_vdd is not None:
        print(f"\nDC operating point:")
        print(f"  VDD = {dc_vdd:.4f} V")
        if dc_vo is not None:
            print(f"  VO  = {dc_vo:.4f} V")

    # AC frequency response
    freq = result.data.get("ac_freq", [])
    vo_mag = result.data.get("ac_VO", [])

    if freq and vo_mag and len(freq) > 1:
        # Convert to dB (relative to DC gain = 1.0)
        vo_db = [20 * math.log10(max(v, 1e-30)) for v in vo_mag]

        # Find -3dB frequency
        f_3dB = None
        for i, db in enumerate(vo_db):
            if db <= -3.0:
                if i > 0:
                    # Linear interpolation
                    f_3dB = freq[i - 1] + (freq[i] - freq[i - 1]) * (-3.0 - vo_db[i - 1]) / (vo_db[i] - vo_db[i - 1])
                else:
                    f_3dB = freq[i]
                break

        expected_f3dB = 1.0 / (2 * math.pi * 1e3 * 50e-15)
        print(f"\nAC frequency response:")
        print(f"  Sweep: {freq[0]:.2e} – {freq[-1]:.2e} Hz ({len(freq)} points)")
        print(f"  |VO| at {freq[0]:.2e} Hz: {vo_mag[0]:.4f} ({vo_db[0]:.1f} dB)")
        print(f"  |VO| at {freq[-1]:.2e} Hz: {vo_mag[-1]:.4f} ({vo_db[-1]:.1f} dB)")
        if f_3dB:
            print(f"  f_3dB = {f_3dB:.3e} Hz ({f_3dB / 1e9:.2f} GHz)")
        print(f"  Expected f_3dB = {expected_f3dB:.3e} Hz ({expected_f3dB / 1e9:.2f} GHz)")

        # Plot frequency response
        _write_plot(freq, vo_db, f_3dB, OUT_DIR / "rc_filter_ac_response.png")
    else:
        print(f"\nAC data not available")
        print(f"  Signals: {sorted(result.data.keys())}")

    # Capacitance from AC current
    i_cap = result.data.get("ac_C0:1", [])
    if freq and i_cap and len(i_cap) > 1:
        idx = min(range(len(freq)), key=lambda i: abs(freq[i] - 1e8))
        if idx > 0:
            df = freq[idx] - freq[idx - 1]
            di = abs(i_cap[idx]) - abs(i_cap[idx - 1])
            cap_val = di / df / (2 * math.pi)
            print(f"\nC0 capacitance (from AC current at {freq[idx]:.2e} Hz):")
            print(f"  C = {cap_val * 1e15:.2f} fF")

    save_summary_json(result, OUT_DIR / "cap_dc_ac_result.json")
    print_timing_summary(result)
    return 0


def _write_plot(freq: list[float], vo_db: list[float], f_3dB: float | None, out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    freq_ghz = np.array(freq) / 1e9

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=160)
    ax.semilogx(freq_ghz, vo_db, linewidth=2, color="#1f77b4", label="|VO| (dB)")
    ax.axhline(-3, color="#d62728", linewidth=1, linestyle="--", alpha=0.7, label="-3 dB")
    if f_3dB:
        ax.axvline(f_3dB / 1e9, color="#2ca02c", linewidth=1, linestyle=":", alpha=0.7)
        ax.annotate(f"f_3dB = {f_3dB / 1e9:.2f} GHz",
                    xy=(f_3dB / 1e9, -3), xytext=(f_3dB / 1e9 * 0.3, -6),
                    fontsize=10, color="#2ca02c",
                    arrowprops=dict(arrowstyle="->", color="#2ca02c", lw=1.2))
    ax.set_xlabel("Frequency (GHz)")
    ax.set_ylabel("Magnitude (dB)")
    ax.set_title("RC Low-Pass Filter — AC Frequency Response")
    ax.legend(loc="lower left")
    ax.grid(True, alpha=0.3)
    ax.set_ylim(min(vo_db) - 2, 2)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"\nPlot saved: {out_path}")


if __name__ == "__main__":
    raise SystemExit(main())

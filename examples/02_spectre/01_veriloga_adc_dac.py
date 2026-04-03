#!/usr/bin/env python3
"""Run the bundled Verilog-A ADC/DAC testbenches with Spectre.

Usage::

    python examples/02_spectre/02_veriloga_adc_dac.py
    python examples/02_spectre/02_veriloga_adc_dac.py --case sine
    python examples/02_spectre/02_veriloga_adc_dac.py --case ramp
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _result_io import print_result_counts, print_timing_summary, save_summary_json, save_waveforms_csv
from virtuoso_bridge.spectre.runner import spectre_mode_args

ROOT = Path(__file__).resolve().parent
ASSET_DIR = ROOT / "assets" / "adc_dac_ideal_4b"

CASE_CONFIG = {
    "sine": {
        "netlist": ASSET_DIR / "tb_adc_dac_ideal_4b_sine.scs",
        "include_files": [
            ASSET_DIR / "adc_ideal_4b.va",
            ASSET_DIR / "dac_ideal_4b.va",
            ASSET_DIR / "sh_ideal.va",
        ],
        "work_dir": ROOT / "output" / "spectre_adc_dac_ideal_4b",
        "plot_path": ROOT / "output" / "spectre_adc_dac_ideal_4b" / "adc_dac_ideal_4b_sine.png",
        "csv_path": ROOT / "output" / "spectre_adc_dac_ideal_4b" / "adc_dac_ideal_4b_sine.csv",
        "json_path": ROOT / "output" / "spectre_adc_dac_ideal_4b" / "adc_dac_ideal_4b_sine_result.json",
        "title": "Spectre Verilog-A ADC/DAC Sine Testbench",
    },
    "ramp": {
        "netlist": ASSET_DIR / "tb_adc_dac_ideal_4b_ramp.scs",
        "include_files": [
            ASSET_DIR / "adc_ideal_4b.va",
            ASSET_DIR / "dac_ideal_4b.va",
        ],
        "work_dir": ROOT / "output" / "spectre_adc_dac_ideal_4b_ramp",
        "plot_path": ROOT / "output" / "spectre_adc_dac_ideal_4b_ramp" / "adc_dac_ideal_4b_ramp.png",
        "csv_path": ROOT / "output" / "spectre_adc_dac_ideal_4b_ramp" / "adc_dac_ideal_4b_ramp.csv",
        "json_path": ROOT / "output" / "spectre_adc_dac_ideal_4b_ramp" / "adc_dac_ideal_4b_ramp_result.json",
        "title": "Spectre Verilog-A ADC/DAC Ramp Testbench",
    },
}

SUPPORTED_MODES = ("spectre", "aps", "x", "cx", "ax", "mx", "lx", "vx")


def _parse_case(argv: list[str]) -> str:
    case = "sine"
    if "--case" in argv:
        idx = argv.index("--case")
        if idx + 1 >= len(argv):
            raise SystemExit("--case requires one of: sine, ramp")
        case = argv[idx + 1].strip().lower()
    if case not in CASE_CONFIG:
        raise SystemExit(f"Unsupported case '{case}'. Use: sine, ramp")
    return case


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


def _bit_values(data: dict[str, list[float]], bit: int) -> list[float]:
    return data.get(f"dout_{bit}", [])


def _write_sine_plot(out_path: Path, title: str, data: dict[str, list[float]]) -> tuple[int, int] | None:
    import matplotlib
    import matplotlib.pyplot as plt
    import numpy as np

    matplotlib.use("Agg")

    time_values = data.get("time", [])
    vin_values = data.get("vin", [])
    vin_sh_values = data.get("vin_sh", [])
    vout_values = data.get("vout", [])
    if not (time_values and vin_values and vin_sh_values and vout_values and all(_bit_values(data, bit) for bit in range(4))):
        return None

    time_ns = np.asarray(time_values, dtype=float) * 1e9
    vin = np.asarray(vin_values, dtype=float)
    vin_sh = np.asarray(vin_sh_values, dtype=float)
    vout = np.asarray(vout_values, dtype=float)
    code = np.zeros_like(time_ns, dtype=int)
    for bit in range(4):
        code += np.rint(np.asarray(_bit_values(data, bit), dtype=float)).astype(int) << bit

    fig, axes = plt.subplots(3, 1, figsize=(10, 7.2), dpi=160, sharex=True, gridspec_kw={"height_ratios": [2, 1.5, 1.5]})
    fig.suptitle(title)
    axes[0].plot(time_ns, vin, linewidth=1.6, label="vin")
    axes[0].plot(time_ns, vin_sh, linewidth=1.4, linestyle=":", label="vin_sh")
    axes[0].plot(time_ns, vout, linewidth=1.6, linestyle="--", label="vout")
    axes[0].set_ylabel("Voltage (V)")
    axes[0].legend(loc="upper right")
    axes[0].grid(True, color="#d9d9d9", linewidth=0.8)
    axes[1].step(time_ns, code, where="post", linewidth=1.6, color="#d62728")
    axes[1].set_ylabel("ADC code")
    axes[1].set_ylim(-0.5, 15.5)
    axes[1].grid(True, color="#d9d9d9", linewidth=0.8)
    axes[2].plot(time_ns, (vout - vin_sh) * 1e3, linewidth=1.4, color="#2ca02c")
    axes[2].axhline(0.0, color="black", linewidth=1.0, linestyle="--")
    axes[2].set_xlabel("Time (ns)")
    axes[2].set_ylabel("vout-vin_sh (mV)")
    axes[2].grid(True, color="#d9d9d9", linewidth=0.8)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return int(code.min()), int(code.max())


def _write_ramp_plot(out_path: Path, title: str, data: dict[str, list[float]]) -> tuple[int, int] | None:
    import matplotlib
    import matplotlib.pyplot as plt
    import numpy as np

    matplotlib.use("Agg")

    time_values = data.get("time", [])
    vin_values = data.get("vin", [])
    clk_values = data.get("clk", [])
    vout_values = data.get("vout", [])
    if not (time_values and vin_values and clk_values and vout_values and all(_bit_values(data, bit) for bit in range(4))):
        return None

    time_ns = np.asarray(time_values, dtype=float) * 1e9
    vin = np.asarray(vin_values, dtype=float)
    clk = np.asarray(clk_values, dtype=float)
    vout = np.asarray(vout_values, dtype=float)
    code = np.zeros_like(time_ns, dtype=int)
    for bit in range(4):
        code += np.rint(np.asarray(_bit_values(data, bit), dtype=float)).astype(int) << bit

    fig, axes = plt.subplots(3, 1, figsize=(10, 7.2), dpi=160, sharex=True, gridspec_kw={"height_ratios": [1.2, 1.5, 2]})
    fig.suptitle(title)
    axes[0].plot(time_ns, clk, linewidth=1.2, color="#7f7f7f")
    axes[0].set_ylabel("clk (V)")
    axes[0].grid(True, color="#d9d9d9", linewidth=0.8)
    axes[1].plot(time_ns, vin, linewidth=1.6, color="#1f77b4", label="vin")
    axes[1].plot(time_ns, vout, linewidth=1.5, linestyle="--", color="#ff7f0e", label="vout")
    axes[1].set_ylabel("Voltage (V)")
    axes[1].legend(loc="upper left")
    axes[1].grid(True, color="#d9d9d9", linewidth=0.8)
    axes[2].step(time_ns, code, where="post", linewidth=1.6, color="#d62728")
    axes[2].set_xlabel("Time (ns)")
    axes[2].set_ylabel("ADC code")
    axes[2].set_ylim(-0.5, 15.5)
    axes[2].grid(True, color="#d9d9d9", linewidth=0.8)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return int(code.min()), int(code.max())


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    case = _parse_case(argv)
    mode = _parse_mode(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    config = CASE_CONFIG[case]
    work_dir: Path = config["work_dir"]
    plot_path: Path = config["plot_path"]
    csv_path: Path = config["csv_path"]
    netlist: Path = config["netlist"]
    include_files: list[Path] = config["include_files"]

    if not netlist.exists():
        print(f"Netlist not found: {netlist}")
        return 1
    missing_files = [path for path in include_files if not path.exists()]
    if missing_files:
        print("Missing Verilog-A include files:")
        for path in missing_files:
            print(f"  {path}")
        return 1

    work_dir.mkdir(parents=True, exist_ok=True)
    spectre_cmd = os.getenv("SPECTRE_CMD", "spectre")
    print(f"[Run] Running Spectre remotely for Verilog-A ADC/DAC case '{case}' ...")

    from virtuoso_bridge.spectre.runner import SpectreSimulator

    sim = SpectreSimulator.from_env(
        spectre_cmd=spectre_cmd,
        spectre_args=spectre_mode_args(mode),
        work_dir=work_dir,
        output_format="psfascii",
    )
    result = sim.run_simulation(netlist, {"include_files": include_files})

    print(f"Status : {result.status.value}")
    print(f"Case   : {case}")
    print_result_counts(result)
    if result.errors:
        print("Errors :")
        for error in result.errors[:8]:
            print(f"  {error}")
    if not result.ok:
        return 1

    print(f"Signals : {sorted(result.data.keys())}")
    time_values = result.data.get("time", [])
    if time_values:
        print("\nFirst 5 waveform points:")
        for idx, t in enumerate(time_values[:5]):
            vin = result.data.get("vin", [])[idx] if idx < len(result.data.get("vin", [])) else float("nan")
            vout = result.data.get("vout", [])[idx] if idx < len(result.data.get("vout", [])) else float("nan")
            print(f"  t={t:.3e}s  vin={vin:.4f}V  vout={vout:.4f}V")

    if case == "sine":
        code_range = _write_sine_plot(plot_path, config["title"], result.data)
    else:
        code_range = _write_ramp_plot(plot_path, config["title"], result.data)
    if code_range is not None:
        print(f"Waveform plot saved to: {plot_path}")
        print(f"Code range: {code_range[0]}..{code_range[1]}")
    save_waveforms_csv(result.data, csv_path)
    print(f"Waveform CSV saved to: {csv_path}")

    save_summary_json(
        result,
        config["json_path"],
        extra={
            "case": case,
            "mode": mode,
            "netlist": str(netlist),
            "include_files": [str(path) for path in include_files],
        },
    )
    print(f"Summary saved to: {config['json_path']}")
    print_timing_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

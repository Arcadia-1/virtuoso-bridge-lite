"""Single-point StrongArm PSS + Pnoise analysis helpers."""

from __future__ import annotations

from pathlib import Path

import matplotlib
import numpy as np
matplotlib.use("Agg")
import matplotlib.pyplot as plt

_trapz = getattr(np, "trapezoid", None) or getattr(np, "trapz", None)


def _trapz_safe(y: np.ndarray, x: np.ndarray) -> float:
    if _trapz is None:
        raise RuntimeError("NumPy trapezoid/trapz is unavailable.")
    return float(_trapz(y, x))

VDD = 0.9
TARGET_TCMP_PS = 50.0
TARGET_NOISE_UV = 400.0


def parse_psf(path: Path) -> dict[str, np.ndarray]:
    from virtuoso_bridge.spectre.parsers import parse_spectre_psf_ascii

    result = parse_spectre_psf_ascii(path)
    if not result.data:
        raise RuntimeError(f"No data in {path}")
    return {key: np.asarray(values, dtype=float) for key, values in result.data.items()}


def first_crossing(
    time_values: np.ndarray,
    signal_values: np.ndarray,
    level: float,
    direction: str = "rising",
) -> float | None:
    for index in range(len(signal_values) - 1):
        a, b = signal_values[index], signal_values[index + 1]
        if direction == "rising" and a < level <= b:
            return float(
                time_values[index]
                + (level - a) / (b - a) * (time_values[index + 1] - time_values[index])
            )
        if direction == "falling" and a > level >= b:
            return float(
                time_values[index]
                + (a - level) / (a - b) * (time_values[index + 1] - time_values[index])
            )
    return None


def extract_metrics(raw_dir: Path, *, vcm: float) -> dict:
    pss_td = raw_dir / "pss.td.pss"
    pnoise_file = raw_dir / "pnoiseMpm0.0.sample.pnoise"

    if not pss_td.exists() or not pnoise_file.exists():
        raise RuntimeError(f"Expected PSF files under {raw_dir}")

    pss = parse_psf(pss_td)
    pnoise = parse_psf(pnoise_file)

    time_values = pss["time"]
    dcmpn = pss["DCMPN"]
    dcmpp = pss["DCMPP"]
    supply_current = pss["V0:p"]
    freq = pnoise["freq"]
    noise_out = pnoise["out"]

    period = float(time_values[-1] - time_values[0])
    avg_current = _trapz_safe(supply_current, time_values) / period
    power_uW = -avg_current * VDD * 1e6

    diff = dcmpn - dcmpp
    level = VDD / 2
    tcmp_raw = first_crossing(time_values, diff, level, "rising")
    tcmprst_raw = first_crossing(time_values, diff, level, "falling")
    tcmp = (tcmp_raw - 5e-12) if tcmp_raw is not None else None
    tcmprst = (tcmprst_raw - 5.05e-10) if tcmprst_raw is not None else None

    mask = (freq >= 0.0) & (freq <= 500e6)
    noise_rms = float(np.sqrt(_trapz_safe(noise_out[mask] ** 2, freq[mask]))) / 50.0

    fom1 = noise_rms**2 * (power_uW * 1e-6) * 1e12
    fom2_u = (noise_rms**2 * (power_uW * 1e-6) * tcmp * 1e18 * 1e6) if tcmp is not None else None

    return {
        "vcm": vcm,
        "power_uW": power_uW,
        "Tcmp_ps": (tcmp * 1e12) if tcmp is not None else None,
        "Tcmprst_ps": (tcmprst * 1e12) if tcmprst is not None else None,
        "Noise_uVrms": noise_rms * 1e6,
        "FOM1": fom1,
        "FOM2_u": fom2_u,
        "pass_Tcmp": ((tcmp * 1e12) <= TARGET_TCMP_PS) if tcmp is not None else None,
        "pass_Noise": (noise_rms * 1e6) <= TARGET_NOISE_UV,
    }


def write_time_domain_plot(raw_dir: Path, out_path: Path) -> Path:
    pss_td = raw_dir / "pss.td.pss"
    pss = parse_psf(pss_td)

    time_ns = np.asarray(pss["time"], dtype=float) * 1e9
    vclk = np.asarray(pss["CLK"], dtype=float)
    vinp = np.asarray(pss["VINP"], dtype=float)
    vinn = np.asarray(pss["VINN"], dtype=float)
    lp = np.asarray(pss["LP"], dtype=float)
    lm = np.asarray(pss["LM"], dtype=float)
    dcmpp = np.asarray(pss["DCMPP"], dtype=float)
    dcmpn = np.asarray(pss["DCMPN"], dtype=float)

    fig, axes = plt.subplots(3, 1, figsize=(10, 7.8), dpi=160, sharex=True)
    fig.suptitle("StrongArm PSS Time-Domain Waveforms")

    axes[0].plot(time_ns, vclk, label="VCLK", color="#111111", linewidth=2.0)
    axes[0].plot(time_ns, vinp, label="VINP", color="#1f77b4", linewidth=1.8)
    axes[0].plot(time_ns, vinn, label="VINN", color="#d62728", linewidth=1.8)
    axes[0].set_ylabel("Input / Clock (V)")
    axes[0].grid(True, color="#d9d9d9", linewidth=0.8)
    axes[0].legend(loc="best")

    axes[1].plot(time_ns, lp, label="LP", color="#2ca02c", linewidth=2.0)
    axes[1].plot(time_ns, lm, label="LM", color="#ff7f0e", linewidth=2.0)
    axes[1].set_ylabel("Latch Nodes (V)")
    axes[1].grid(True, color="#d9d9d9", linewidth=0.8)
    axes[1].legend(loc="best")

    axes[2].plot(time_ns, dcmpp, label="DCMPP", color="#9467bd", linewidth=2.0)
    axes[2].plot(time_ns, dcmpn, label="DCMPN", color="#8c564b", linewidth=2.0)
    axes[2].set_xlabel("Time (ns)")
    axes[2].set_ylabel("Buffered Out (V)")
    axes[2].grid(True, color="#d9d9d9", linewidth=0.8)
    axes[2].legend(loc="best")

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path

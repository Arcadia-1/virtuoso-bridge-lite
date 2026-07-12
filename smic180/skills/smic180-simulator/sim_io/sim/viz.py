"""
Simulation Result Visualization — Parse ASCII PSF + Generate SVG/PNG Plots.

Reads spectre ASCII PSF output files and produces:
  - DC sweep: voltage transfer curve
  - AC analysis: Bode plot (gain & phase vs frequency)
  - TRAN analysis: waveform plot
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class DCSweepData:
    sweep_var: str
    sweep_values: list[float] = field(default_factory=list)
    signals: dict[str, list[float]] = field(default_factory=dict)


@dataclass
class ACSweepData:
    freq: list[float] = field(default_factory=list)
    signals: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    # signals[name] = [(real, imag), ...]


@dataclass
class TranData:
    time: list[float] = field(default_factory=list)
    signals: dict[str, list[float]] = field(default_factory=dict)


@dataclass
class NoiseData:
    freq: list[float] = field(default_factory=list)
    signals: dict[str, list[float]] = field(default_factory=dict)


@dataclass
class OperatingPointData:
    instances: dict[str, dict[str, float]] = field(default_factory=dict)


# ── ASCII PSF Parser ──────────────────────────────────────────────

def parse_psf_ascii(path: str | Path) -> DCSweepData | ACSweepData | TranData | NoiseData | OperatingPointData:
    """Parse an ASCII PSF file into structured data."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")

    # Detect analysis type from header
    analysis_type = "unknown"
    for line in text.splitlines():
        if '"analysis type"' in line:
            m = re.search(r'"analysis type"\s+"(\w+)"', line)
            if m:
                analysis_type = m.group(1)
            break

    if analysis_type == "dc":
        return _parse_dc_psf(text)
    elif analysis_type == "ac":
        return _parse_ac_psf(text)
    elif analysis_type == "tran":
        return _parse_tran_psf(text)
    elif analysis_type == "noise":
        return _parse_noise_psf(text)
    elif analysis_type.lower() in ("dcop", "op") or (analysis_type.lower() == "info" and re.search(r'"AnalysisType"\s+"op"', text, re.I)):
        return _parse_oppoint_psf(text)
    else:
        raise ValueError(f"Unsupported analysis type: {analysis_type}")


def _parse_noise_psf(text: str) -> NoiseData:
    data = NoiseData()
    aliases = {}
    in_trace = False
    in_value = False
    pending_group = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "TRACE":
            in_trace = True
            continue
        if line == "VALUE":
            in_trace = False
            in_value = True
            continue
        if line == "END":
            break
        if in_trace:
            group = re.match(r'^"([^"]+)"\s+GROUP\b', line)
            if group:
                pending_group = group.group(1)
                continue
            trace = re.match(r'^"([^"]+)"\s+"[^"]+"', line)
            if trace and pending_group is not None:
                aliases[pending_group] = trace.group(1)
                pending_group = None
            continue
        if not in_value:
            continue
        match = re.match(r'^"([^"]+)"\s+([\d.eE+\-]+)', line)
        if not match:
            continue
        name, value = match.group(1), float(match.group(2))
        if name.lower() == "freq":
            data.freq.append(value)
        else:
            data.signals.setdefault(aliases.get(name, name), []).append(value)
    return data


def _parse_oppoint_psf(text: str) -> OperatingPointData:
    data = OperatingPointData()
    struct_fields: dict[str, list[str]] = {}
    lines = [line.strip() for line in text.splitlines()]
    in_type = False
    model_name = None
    depth = 0
    for line in lines:
        if line == "TYPE":
            in_type = True
            continue
        if line == "VALUE":
            break
        if not in_type:
            continue
        model = re.match(r'^"([^"]+)"\s+STRUCT\($', line)
        if model and depth == 0:
            model_name = model.group(1)
            struct_fields[model_name] = []
            depth = 1
            continue
        if model_name is not None and depth == 1:
            field_match = re.match(r'^"([^"]+)"\s+(?:FLOAT|INT|STRING)\b', line)
            if field_match:
                field_name = field_match.group(1).lower()
                struct_fields[model_name].append("id" if field_name == "ids" else field_name)
        depth += line.count('(') - line.count(')')
        if model_name is not None and depth == 0:
            model_name = None

    in_value = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line == "VALUE":
            in_value = True
            index += 1
            continue
        if line == "END":
            break
        if not in_value:
            index += 1
            continue
        scalar = re.match(r'^"([^"]+):([^"\s]+)"(?:\s+"[^"]+")?\s+([\d.eE+\-]+)', line)
        if scalar:
            instance, field_name, value = scalar.groups()
            instance = re.sub(r'^DUT[./]', '', instance, flags=re.I)
            data.instances.setdefault(instance, {})[field_name.lower()] = float(value)
            index += 1
            continue
        record = re.match(r'^"([^"]+)"\s+"([^"]+)"\s+\($', line)
        if not record:
            index += 1
            continue
        instance, record_type = record.groups()
        values = []
        index += 1
        while index < len(lines) and not lines[index].startswith(')'):
            if re.fullmatch(r'(?:NaN|[\d.eE+\-]+)', lines[index], re.I):
                values.append(float(lines[index]))
            index += 1
        fields = struct_fields.get(record_type, [])
        instance = re.sub(r'^DUT[./]', '', instance, flags=re.I)
        data.instances.setdefault(instance, {}).update(dict(zip(fields, values)))
        index += 1
    return data


def _parse_dc_psf(text: str) -> DCSweepData:
    # Extract sweep variable name from SWEEP section
    sweep_var = "VDD"
    m = re.search(r'SWEEP\s+"(\w+)"\s+"sweep"', text)
    if m:
        sweep_var = m.group(1)

    # Extract trace names from TRACE section. Spectre can write GROUP
    # aliases and then use those aliases in VALUE, so map aliases back
    # to human-readable signal names.
    trace_names = []
    value_name_map = {}
    in_trace = False
    pending_group = None
    for line in text.splitlines():
        line = line.strip()
        if line == "TRACE":
            in_trace = True
            continue
        if line in ("VALUE", "SWEEP", "HEADER", "TYPE", "END"):
            in_trace = False
            continue
        if in_trace:
            m_group = re.match(r'^"([^"]+)"\s+GROUP\b', line)
            if m_group:
                pending_group = m_group.group(1)
                continue
            m = re.match(r'^"(\w[^"]*)"\s+"(\w+)"', line)
            if m:
                name = m.group(1)
                trace_names.append(name)
                value_name_map[name] = name
                if pending_group is not None:
                    value_name_map[pending_group] = name
                    pending_group = None

    # Each data block has (1 sweep_var + N traces) entries, where N = len(trace_names)
    # But the sweep_var also appears as a trace signal, so the block size in VALUE
    # is len(trace_names) + 1 (sweep value + all trace values including sweep readback)
    block_size = len(trace_names) + 1

    # Parse VALUE section
    data = DCSweepData(sweep_var=sweep_var)
    in_value = False
    entry_idx = 0

    for line in text.splitlines():
        line = line.strip()
        if line == "VALUE":
            in_value = True
            entry_idx = 0
            continue
        if line == "END":
            break
        if not in_value:
            continue

        # Scalar: "name" value
        m = re.match(r'^"(\w[^"]*)"\s+([\d.eE+\-]+)$', line)
        if m:
            name, val = value_name_map.get(m.group(1), m.group(1)), float(m.group(2))

            if entry_idx == 0:
                # First entry in each block is always the sweep value
                data.sweep_values.append(val)
            else:
                if name not in data.signals:
                    data.signals[name] = []
                data.signals[name].append(val)

            entry_idx += 1
            if entry_idx >= block_size:
                entry_idx = 0

    return data


def _parse_ac_psf(text: str) -> ACSweepData:
    data = ACSweepData()
    in_value = False

    for line in text.splitlines():
        line = line.strip()
        if line == "VALUE":
            in_value = True
            continue
        if line == "END":
            break
        if not in_value:
            continue

        # "freq" 1.0
        m = re.match(r'^"freq"\s+([\d.eE+\-]+)$', line)
        if m:
            data.freq.append(float(m.group(1)))
            continue

        # "VOUT" (19.23 -5.2e-08)
        m = re.match(r'^"(\w[^"]*)"\s+\(([\d.eE+\-]+)\s+([\d.eE+\-]+)\)$', line)
        if m:
            name = m.group(1)
            real, imag = float(m.group(2)), float(m.group(3))
            if name not in data.signals:
                data.signals[name] = []
            data.signals[name].append((real, imag))

    return data


def _parse_tran_psf(text: str) -> TranData:
    data = TranData()
    in_value = False
    value_name_map = _trace_value_name_map(text)
    trace_names = [name for alias, name in value_name_map.items() if alias == name and name != "time"]
    current: dict[str, float] = {}
    pending_time: float | None = None

    def flush_sample() -> None:
        if pending_time is None:
            return
        data.time.append(pending_time)
        for sig in trace_names:
            data.signals.setdefault(sig, []).append(current.get(sig, math.nan))

    for line in text.splitlines():
        line = line.strip()
        if line == "VALUE":
            in_value = True
            continue
        if line == "END":
            break
        if not in_value:
            continue

        m = re.match(r'^"([^"]+)"\s+([\d.eE+\-]+)$', line)
        if m:
            name, val = value_name_map.get(m.group(1), m.group(1)), float(m.group(2))
            if name == "time":
                flush_sample()
                pending_time = val
            else:
                current[name] = val

    flush_sample()

    return data


def _trace_value_name_map(text: str) -> dict[str, str]:
    value_name_map: dict[str, str] = {}
    in_trace = False
    pending_group = None
    for line in text.splitlines():
        line = line.strip()
        if line == "TRACE":
            in_trace = True
            continue
        if line in ("VALUE", "SWEEP", "HEADER", "TYPE", "END"):
            if in_trace:
                break
            continue
        if not in_trace:
            continue
        m_group = re.match(r'^"([^"]+)"\s+GROUP\b', line)
        if m_group:
            pending_group = m_group.group(1)
            continue
        m = re.match(r'^"([^"]+)"\s+"[^"]+"', line)
        if m:
            name = m.group(1)
            value_name_map[name] = name
            if pending_group is not None:
                value_name_map[pending_group] = name
                pending_group = None
    return value_name_map


# ── Plot Generation ───────────────────────────────────────────────

def plot_dc_sweep(
    dc: DCSweepData,
    output_path: str | Path,
    signals: Optional[list[str]] = None,
    labels: Optional[dict[str, str]] = None,
    title: str = "DC Sweep",
) -> Path:
    """Plot DC sweep results as SVG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sigs = signals or [s for s in ("VOUT", "VIP", "VIN") if s in dc.signals]
    if not sigs:
        sigs = list(dc.signals.keys())[:4]

    fig, ax = plt.subplots(figsize=(8, 5))
    for sig in sigs:
        if sig in dc.signals:
            ax.plot(dc.sweep_values, dc.signals[sig], label=(labels or {}).get(sig, sig), linewidth=1.5)

    ax.set_xlabel(f"{dc.sweep_var} (V)")
    ax.set_ylabel("Voltage (V)")
    ax.set_title(title)
    if ax.get_legend_handles_labels()[0]:
        ax.legend()
    ax.grid(True, alpha=0.3)

    out = Path(output_path)
    fig.savefig(out, format="svg", bbox_inches="tight")
    plt.close(fig)
    return out


def plot_ac_bode(
    ac: ACSweepData,
    output_path: str | Path,
    signals: Optional[list[str]] = None,
    labels: Optional[dict[str, str]] = None,
    title: str = "AC Analysis — Bode Plot",
) -> Path:
    """Plot AC Bode plot (magnitude + phase) as SVG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    sigs = signals or [s for s in ("VOUT", "VIP", "VIN") if s in ac.signals]
    if not sigs:
        sigs = list(ac.signals.keys())[:4]

    freq = np.array(ac.freq)
    freq_khz = freq / 1e3
    freq_ghz = freq / 1e9

    fig, (ax_mag, ax_phase) = plt.subplots(2, 1, figsize=(8, 7), sharex=True)

    for sig in sigs:
        if sig not in ac.signals:
            continue
        vals = np.array(ac.signals[sig])
        mag = np.abs(vals)
        phase = np.degrees(np.angle(vals))

        label = (labels or {}).get(sig, sig)
        ax_mag.semilogx(freq, 20 * np.log10(mag + 1e-30), label=label, linewidth=1.5)
        ax_phase.semilogx(freq, phase, label=label, linewidth=1.5)

    ax_mag.set_ylabel("Magnitude (dB)")
    ax_mag.set_title(title)
    if ax_mag.get_legend_handles_labels()[0]:
        ax_mag.legend()
    ax_mag.grid(True, alpha=0.3, which="both")

    ax_phase.set_xlabel("Frequency (Hz)")
    ax_phase.set_ylabel("Phase (deg)")
    ax_phase.grid(True, alpha=0.3, which="both")

    out = Path(output_path)
    fig.savefig(out, format="svg", bbox_inches="tight")
    plt.close(fig)
    return out


def plot_tran(
    tran: TranData,
    output_path: str | Path,
    signals: Optional[list[str]] = None,
    labels: Optional[dict[str, str]] = None,
    title: str = "Transient Analysis",
) -> Path:
    """Plot transient waveforms as SVG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if signals:
        sigs = signals
    else:
        # Filter out constant signals (ground nets, static supplies)
        # and prioritize signals with significant variation
        varying_sigs = []
        static_sigs = []
        for sig_name, sig_data in tran.signals.items():
            if len(sig_data) < 2:
                continue
            sig_range = max(sig_data) - min(sig_data)
            if sig_range > 1e-6:  # non-constant
                varying_sigs.append((sig_name, sig_range))
            else:
                static_sigs.append(sig_name)
        # Sort by range (most varying first)
        varying_sigs.sort(key=lambda x: x[1], reverse=True)
        sigs = [s[0] for s in varying_sigs[:12]]
        # Add a few static signals if space
        for s in static_sigs[:3]:
            if len(sigs) < 15:
                sigs.append(s)

    n_sigs = len(sigs)
    n_cols = min(3, n_sigs) if n_sigs > 6 else 1
    n_rows = (n_sigs + n_cols - 1) // n_cols if n_cols > 1 else 1

    if n_cols == 1:
        fig, ax = plt.subplots(figsize=(10, max(4, n_sigs * 0.8)))
        for sig in sigs:
            if sig in tran.signals:
                ax.plot(tran.time, tran.signals[sig], label=(labels or {}).get(sig, sig), linewidth=1.2)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Voltage (V)")
        ax.set_title(title)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
    else:
        fig, axes = plt.subplots(n_rows, n_cols, figsize=(14, 3 * n_rows), squeeze=False)
        for idx, sig in enumerate(sigs):
            row, col = idx // n_cols, idx % n_cols
            ax = axes[row][col]
            if sig in tran.signals:
                label = (labels or {}).get(sig, sig)
                ax.plot(tran.time, tran.signals[sig], label=label, linewidth=1.0)
                ax.set_title(label, fontsize=9)
                ax.set_ylabel("V", fontsize=8)
                ax.grid(True, alpha=0.3)
                ax.tick_params(labelsize=7)
        # Hide unused subplots
        for idx in range(len(sigs), n_rows * n_cols):
            row, col = idx // n_cols, idx % n_cols
            axes[row][col].set_visible(False)
        fig.tight_layout()

    out = Path(output_path)
    fig.savefig(out, format="svg", bbox_inches="tight")
    plt.close(fig)
    return out


def visualize_run(
    psf_dir: str | Path,
    output_dir: Optional[str | Path] = None,
    signals: Optional[list[str]] = None,
    labels: Optional[dict[str, str]] = None,
) -> list[Path]:
    """Auto-detect and plot all PSF results in a directory.

    Returns list of generated plot file paths.
    """
    psf_dir = Path(psf_dir)
    output_dir = Path(output_dir) if output_dir else psf_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    plots = []

    for psf_file in sorted(psf_dir.glob("*.*")):
        name = psf_file.stem  # e.g. "dc", "ac"
        ext = psf_file.suffix  # e.g. ".dc", ".ac"

        # Only parse analysis result files, not info files
        if ext in (".info", ".primitives", ".subckts") or name == "logFile":
            continue
        if not psf_file.is_file():
            continue

        try:
            data = parse_psf_ascii(psf_file)
        except Exception as e:
            print(f"[viz] Skipping {psf_file.name}: {e}")
            continue

        if isinstance(data, DCSweepData):
            out = output_dir / f"{name}_dc_sweep.svg"
            plot_dc_sweep(data, out, signals=signals, labels=labels, title=f"DC Sweep — {name}")
            plots.append(out)
            print(f"[viz] DC sweep plot: {out}")
        elif isinstance(data, ACSweepData):
            out = output_dir / f"{name}_bode.svg"
            plot_ac_bode(data, out, signals=signals, labels=labels, title=f"AC Bode — {name}")
            plots.append(out)
            print(f"[viz] AC Bode plot: {out}")
        elif isinstance(data, TranData):
            out = output_dir / f"{name}_tran.svg"
            plot_tran(data, out, signals=signals, labels=labels, title=f"Transient — {name}")
            plots.append(out)
            print(f"[viz] Transient plot: {out}")

    return plots


# ── Key Measurement Extraction ────────────────────────────────────

def extract_dc_metrics(dc: DCSweepData) -> dict:
    """Extract key DC metrics from sweep data."""
    metrics = {}
    if "VOUT" in dc.signals and dc.sweep_values:
        vout = dc.signals["VOUT"]
        # Find VOUT at nominal VDD (closest to 1.8)
        nominal_idx = min(range(len(dc.sweep_values)),
                          key=lambda i: abs(dc.sweep_values[i] - 1.8))
        metrics["VOUT_at_VDD1.8"] = vout[nominal_idx]

        # DC gain: dVOUT/dVDD around operating point
        if nominal_idx > 0 and nominal_idx < len(vout) - 1:
            dvout = vout[nominal_idx + 1] - vout[nominal_idx - 1]
            dvdd = dc.sweep_values[nominal_idx + 1] - dc.sweep_values[nominal_idx - 1]
            metrics["DC_gain_VDD"] = dvout / dvdd if dvdd != 0 else None

        # Voltage range
        metrics["VOUT_min"] = min(vout)
        metrics["VOUT_max"] = max(vout)
    return metrics


def extract_ac_metrics(ac: ACSweepData) -> dict:
    """Extract key AC metrics (gain, bandwidth, phase margin)."""
    metrics = {}
    if "VOUT" in ac.signals and ac.freq:
        vout_mag = [math.sqrt(r**2 + i**2) for r, i in ac.signals["VOUT"]]
        vout_phase = [math.degrees(math.atan2(i, r)) for r, i in ac.signals["VOUT"]]

        # DC gain (first frequency point)
        dc_gain_mag = vout_mag[0]
        metrics["DC_gain_dB"] = 20 * math.log10(dc_gain_mag) if dc_gain_mag > 0 else None
        metrics["DC_gain_V_V"] = dc_gain_mag

        # -3dB bandwidth
        if metrics["DC_gain_dB"] is not None:
            target_db = metrics["DC_gain_dB"] - 3.0
            for i in range(1, len(vout_mag)):
                current_db = 20 * math.log10(vout_mag[i]) if vout_mag[i] > 0 else -200
                if current_db <= target_db:
                    # Linear interpolation
                    f1, f2 = ac.freq[i - 1], ac.freq[i]
                    db1 = 20 * math.log10(vout_mag[i - 1])
                    db2 = current_db
                    frac = (target_db - db1) / (db2 - db1) if db2 != db1 else 0.5
                    metrics["BW_3dB_Hz"] = f1 + frac * (f2 - f1)
                    metrics["BW_3dB"] = _format_freq(metrics["BW_3dB_Hz"])
                    break

        # Phase at DC gain
        metrics["phase_at_DC"] = vout_phase[0]

    return metrics


def _format_freq(hz: float) -> str:
    if hz >= 1e9:
        return f"{hz / 1e9:.2f} GHz"
    elif hz >= 1e6:
        return f"{hz / 1e6:.2f} MHz"
    elif hz >= 1e3:
        return f"{hz / 1e3:.2f} kHz"
    else:
        return f"{hz:.2f} Hz"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python sim_viz.py <psf_dir> [output_dir] [signal1,signal2,...]")
        sys.exit(1)

    psf_dir = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else None
    sigs = sys.argv[3].split(",") if len(sys.argv) > 3 else None

    plots = visualize_run(psf_dir, out_dir, signals=sigs)
    print(f"\nGenerated {len(plots)} plot(s)")

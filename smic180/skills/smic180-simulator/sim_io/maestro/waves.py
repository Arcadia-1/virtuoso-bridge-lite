"""Parse Maestro-exported waveform text files and generate SVG plots.

After run_maestro_sim() completes, it writes per-signal voltage waveforms
to <run_dir>/maestro_waves/<signal>.txt via OCEAN ocnPrint.  Each file
contains two columns:  time (s)  value (V).

This module parses those files and calls the existing plot_tran() from
sim_io.sim.viz to produce a single SVG covering all exported signals.
"""

from __future__ import annotations

from pathlib import Path

from sim_io.sim.viz import TranData, plot_tran


def _parse_two_col_text(text: str) -> tuple[list[float], list[float]]:
    """Parse OCEAN ocnPrint output (two numeric columns) into (xs, ys)."""
    xs: list[float] = []
    ys: list[float] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            xs.append(float(parts[0]))
            ys.append(float(parts[1]))
        except ValueError:
            continue
    return xs, ys


def plot_maestro_waves(waves_dir: Path, plots_dir: Path) -> list[Path]:
    """Parse maestro_waves/*.txt files and generate a transient SVG plot.

    All signals share the time axis from the first successfully parsed file.
    Signals with a different number of samples than the reference time axis
    are skipped with a warning.

    Returns list of generated SVG file paths (empty if no data found).
    """
    waves_dir = Path(waves_dir)
    plots_dir = Path(plots_dir)
    plots_dir.mkdir(parents=True, exist_ok=True)

    if not waves_dir.exists():
        print("[maestro-waves] maestro_waves/ not found — no waveform files to plot")
        return []

    tran = TranData()
    ref_len = 0

    for txt_file in sorted(waves_dir.glob("*.txt")):
        text = txt_file.read_text(encoding="utf-8", errors="replace")
        xs, ys = _parse_two_col_text(text)
        if not xs or not ys:
            print(f"[maestro-waves] Skipping {txt_file.name}: empty or unparseable")
            continue

        if not tran.time:
            tran.time = xs
            ref_len = len(xs)

        if len(ys) != ref_len:
            print(f"[maestro-waves] Skipping {txt_file.name}: "
                  f"{len(ys)} samples vs reference {ref_len}")
            continue

        sig_name = txt_file.stem
        tran.signals[sig_name] = ys
        print(f"[maestro-waves] Parsed {sig_name}: {len(ys)} points")

    if not tran.signals:
        print("[maestro-waves] No waveform data found in maestro_waves/")
        return []

    out = plots_dir / "tran_maestro.svg"
    plot_tran(tran, out, title="Transient — Maestro")
    print(f"[maestro-waves] SVG: {out}")
    return [out]

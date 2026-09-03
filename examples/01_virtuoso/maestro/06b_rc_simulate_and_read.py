#!/usr/bin/env python3
"""Step 2: Simulate (GUI mode) → wait → read results → export waveforms.

Opens an editable Maestro GUI session, the supported mode for simulation and
result evaluation. ``run_and_wait`` keeps the SKILL channel free after Cadence
accepts the run so blocking dialogs can be diagnosed through the X11 rescue
commands.

Prerequisite: run 06a_rc_create.py first; copy the cell name it prints.

Usage::

    python 06b_rc_simulate_and_read.py <LIB> <CELL>

Example::

    python 06b_rc_simulate_and_read.py PLAYGROUND_LLM TB_RC_FILTER_20260430_120000
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient


def parse_wave_file(path: str) -> list[tuple[float, float]]:
    pairs = []
    for line in Path(path).read_text().splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            try:
                pairs.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return pairs


def main() -> int:
    if len(sys.argv) < 3:
        print("=" * 60, file=sys.stderr)
        print(" ERROR: missing required arguments <LIB> <CELL>", file=sys.stderr)
        print()
        print(
            f" Usage: python {Path(__file__).name} <LIB> <CELL>\n"
            " Example: python 06b_rc_simulate_and_read.py PLAYGROUND_LLM TB_RC_FILTER_20260430_120000\n"
            " (CELL is the timestamped name printed by 06a_rc_create.py.)\n",
            file=sys.stderr,
        )
        print(
            " NOTE: Running this script from VSCode (Ctrl+F5 / F5) will NOT\n"
            "       work — VSCode does not pass command-line arguments by default.\n",
            file=sys.stderr,
        )
        print("=" * 60, file=sys.stderr)
        return 1

    lib, cell = sys.argv[1], sys.argv[2]

    client = VirtuosoClient.from_env()
    print(f"[info] {lib}/{cell}")
    t_total = time.time()

    # 1. Open or reuse the editable GUI session for this exact Maestro cell.
    #    The helper closes conflicting background/GUI sessions safely.
    session = client.maestro.open_gui_session(lib, cell)
    print(f"[session] {session} (GUI)")

    try:
        # 2. Run + wait.  run_and_wait registers a SKILL completion callback
        # atomically with maeRunSimulation, then polls a marker file via SSH —
        # so the SKILL channel stays free during the wait.
        t0 = time.time()
        history, _status = client.maestro.run_and_wait(session=session, timeout=600)
        history_name = history.strip('"')
        print(f"[sim] Done: {history_name} ({time.time() - t0:.1f}s)")

        # 3. Read structured results (per point × per output, with spec/pass).
        print("\n=== Results ===")
        results = client.maestro.read_results(
            session, lib=lib, cell=cell, history=history_name
        )
        print(f"History: {history_name}")
        for pt in results.get("points", []):
            pn = pt.get("point", "?")
            params = pt.get("parameters", {}) or {}
            param_str = ", ".join(f"{k}={v}" for k, v in params.items())
            print(f"\nPoint {pn}" + (f"  ({param_str})" if param_str else ""))
            for out_name, info in (pt.get("outputs", {}) or {}).items():
                val = info.get("value", "")
                spec = info.get("spec", "")
                pf = info.get("pass_fail", "")
                line = f"  {out_name} = {val}"
                if spec:
                    tag = f", {pf}" if pf else ""
                    line += f"  [spec: {spec}{tag}]"
                print(line)
        if results.get("overall_spec"):
            print(f"\nOverall spec: {results['overall_spec']}")
        if results.get("overall_yield"):
            print(f"Overall yield: {results['overall_yield']}")

        # 4. Export waveforms via OCEAN from the same explicit history.
        if history_name:
            output_dir = Path(__file__).parent / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            print("\n=== Waveforms ===")
            mag_file = str(output_dir / "rc_ac_mag_db.txt")
            client.maestro.export_waveform(session, 'dB20(mag(v("/OUT")))',
                            mag_file, analysis="ac", history=history_name)
            print(f"AC magnitude: {mag_file}")

            phase_file = str(output_dir / "rc_ac_phase.txt")
            client.maestro.export_waveform(session, 'phase(v("/OUT"))',
                            phase_file, analysis="ac", history=history_name)
            print(f"AC phase: {phase_file}")

            data = parse_wave_file(mag_file)
            if data:
                print(f"\n=== {len(data)} frequency points ===")
                for target in [1e6, 1e8, 1e9, 1e10]:
                    closest = min(data, key=lambda p: abs(p[0] - target))
                    print(f"  {target:.0e} Hz: {closest[1]:.2f} dB")
                for i, (f, db) in enumerate(data):
                    if db <= -3.0:
                        if i > 0:
                            f_prev, db_prev = data[i - 1]
                            ratio = (-3.0 - db_prev) / (db - db_prev)
                            f_3db = f_prev + ratio * (f - f_prev)
                        else:
                            f_3db = f
                        print(f"  f_3dB = {f_3db:.3e} Hz")
                        break
    finally:
        # 5. Always release the GUI session, even on error.
        try:
            client.maestro.close_gui_session(session, save=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] close_gui_session failed: {exc}", file=sys.stderr)

    print(f"[total] {time.time() - t_total:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

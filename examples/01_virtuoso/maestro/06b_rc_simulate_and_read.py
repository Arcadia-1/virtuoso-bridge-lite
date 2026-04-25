#!/usr/bin/env python3
"""Step 2: Simulate → wait → read results → export waveforms.

Reuses existing Maestro GUI if open, otherwise opens fresh.
Can be run multiple times — each run starts a new simulation.

Prerequisite: run 06a_rc_create.py first.

Usage::

    python 06b_rc_simulate_and_read.py <LIB>

    <LIB> is required — must match the library used in 06a_rc_create.py.
    Example::

        python 06b_rc_simulate_and_read.py testlib
"""

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import (
    read_results, export_waveform, save_setup, run_and_wait,
)

CELL = "TB_RC_FILTER"


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


def ensure_gui(client: VirtuosoClient, lib: str) -> None:
    """Make sure maestro GUI is open and editable. Reuse if possible."""
    # Check for existing valid session
    r = client.execute_skill('''
let((s) s = nil
  foreach(x maeGetSessions() unless(s when(maeGetSetup(?session x) s = x)))
  s)
''')
    session = (r.output or "").strip('"')

    if session and session != "nil":
        # Session exists — just save to keep it clean
        save_setup(client, lib, CELL)
        return

    # No valid session — open fresh
    client.execute_skill(
        f'deOpenCellView("{lib}" "{CELL}" "maestro" "maestro" nil "r")')
    client.execute_skill('maeMakeEditable()')


def main() -> int:
    if len(sys.argv) < 2:
        print("=" * 60, file=sys.stderr)
        print(" ERROR: missing required argument <LIB>", file=sys.stderr)
        print()
        print(
            f" Usage: python {Path(__file__).name} <LIB>\n"
            " Example: python 06b_rc_simulate_and_read.py lifangshi\n",
            file=sys.stderr,
        )
        print(
            " NOTE: Running this script from VSCode (Ctrl+F5 / F5) will NOT\n"
            "       work — VSCode does not pass command-line arguments by default.\n",
            file=sys.stderr,
        )
        print("=" * 60, file=sys.stderr)
        return 1

    lib = sys.argv[1]

    client = VirtuosoClient.from_env()
    print(f"[info] {lib}/{CELL}")
    t_total = time.time()

    # 1. Ensure GUI is open
    ensure_gui(client, lib)
    print("[gui] Ready")

    # 2. Run simulation + wait.  run_and_wait registers a SKILL completion
    # callback atomically with maeRunSimulation, then polls a marker file via
    # SSH — so the SKILL channel stays free during the wait.
    t0 = time.time()
    try:
        history, _status = run_and_wait(client, timeout=600)
    except RuntimeError:
        print("[sim] run_and_wait failed — session may be stale, closing + reopening...")
        client.execute_skill(f'''
foreach(s maeGetSessions()
  errset(maeSaveSetup(?lib "{lib}" ?cell "{CELL}" ?view "maestro" ?session s))
  errset(maeCloseSession(?session s ?forceClose t)))
foreach(win hiGetWindowList()
  let((n) n = hiGetWindowName(win)
    when(and(n rexMatchp("maestro" n))
      errset(hiCloseWindow(win))
      let((form) form = hiGetCurrentForm() when(form errset(hiFormCancel(form)))))))
t
''')
        time.sleep(1)
        client.execute_skill(
            f'deOpenCellView("{lib}" "{CELL}" "maestro" "maestro" nil "r")')
        client.execute_skill('maeMakeEditable()')
        history, _status = run_and_wait(client, timeout=600)

    print(f"[sim] Done: {history} ({time.time() - t0:.1f}s)")

    # 4. Find session and read results
    r = client.execute_skill('''
let((s) s = nil
  foreach(x maeGetSessions() unless(s when(maeGetSetup(?session x) s = x)))
  s)
''')
    session = (r.output or "").strip('"')

    print("\n=== Results ===")
    results = read_results(client, session, lib=lib, cell=CELL)
    if results:
        for key, (expr, raw) in results.items():
            print(f"[{key}] {expr}")
            print(f"  {raw}")

    # 5. Export waveforms
    yield_expr = results.get("maeGetOverallYield", ("", ""))[0]
    hm = re.search(r'Interactive\.\d+', yield_expr)
    history = hm.group(0) if hm else ""

    if history:
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        print("\n=== Waveforms ===")
        mag_file = str(output_dir / "rc_ac_mag_db.txt")
        export_waveform(client, session, 'dB20(mag(v("/OUT")))',
                        mag_file, analysis="ac", history=history)
        print(f"AC magnitude: {mag_file}")

        phase_file = str(output_dir / "rc_ac_phase.txt")
        export_waveform(client, session, 'phase(v("/OUT"))',
                        phase_file, analysis="ac", history=history)
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

        # 6. Restore latest history in GUI + save
        client.execute_skill(f'maeRestoreHistory("{history}")')
        save_setup(client, lib, CELL)
        print(f"\n[gui] Showing {history}")

    print(f"[total] {time.time() - t_total:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

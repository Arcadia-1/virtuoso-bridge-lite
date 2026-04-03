#!/usr/bin/env python3
"""Create an RC low-pass filter, run AC analysis, and compare C=1pF vs C=100fF.

End-to-end example:
1. Create schematic (vdc source + R + C + GND)
2. Set component parameters via CDF
3. Create Maestro view with AC analysis + parametric sweep on C
4. Run simulation (single run covers both C values)
5. Read & compare the AC magnitude responses

Prerequisites:
- virtuoso-bridge tunnel running
- Virtuoso with analogLib available

Expected result:
    f_3dB = 1 / (2 * pi * R * C)
    C = 1 pF  ->  f_3dB ~  159 MHz
    C = 100 fF -> f_3dB ~ 1.59 GHz
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

LIB = "PLAYGROUND_LLM"
CELL = "TB_RC_FILTER_SWEEP"
C_VALUES = ["1p", "100f"]   # comma-separated in Maestro for parametric sweep

REMOTE_TMP = "/tmp/rc_filter_ac"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def skill(client: VirtuosoClient, expr: str, **kw) -> "VirtuosoResult":
    """Shorthand for execute_skill with error checking."""
    r = client.execute_skill(expr, **kw)
    return r


def set_cdf_param(client: VirtuosoClient, cv_var: str, inst_name: str,
                  param: str, value: str) -> None:
    """Set a CDF parameter on a schematic instance.

    Uses ``cdfFindParamByName(cdfGetInstCDF(inst), param)~>value = value``.
    """
    r = client.execute_skill(
        f'cdfFindParamByName(cdfGetInstCDF('
        f'car(setof(i {cv_var}~>instances i~>name == "{inst_name}")))'
        f' "{param}")~>value = "{value}"'
    )
    if r.errors:
        raise RuntimeError(f"Failed to set {inst_name}.{param}: {r.errors[0]}")


def parse_ocnprint_sets(text: str) -> dict[str, list[tuple[float, float]]]:
    """Parse ocnPrint output into per-sweep-value datasets.

    Handles two formats that Maestro produces:

    1. **Column format** (parametric sweep) — one header row with sweep
       values, then freq + N columns of data side by side::

           freq (Hz)      dB20(...) dB20(...)
           c_val          1.000e-13 1.000e-12
           1.00000e+00    0.00e+00  0.00e+00

    2. **Set format** (corners / sequential runs) — separate blocks::

           # Set No. 1
           (c_val = 1.000e-12)
           freq (Hz)  dB20(...)
           1.00e+00   0.00e+00

    Returns ``{sweep_value_str: [(freq, value), ...]}``.
    """
    lines = text.strip().splitlines()

    # --- Detect column format (sweep values on a header line) ---
    sweep_values: list[str] = []
    data_start = 0
    for i, line in enumerate(lines):
        parts = line.split()
        if not parts:
            continue
        # Look for the sweep variable header: "c_val  1.000e-13  1.000e-12"
        if parts[0].isidentifier() and len(parts) >= 2:
            try:
                vals = [float(p) for p in parts[1:]]
                sweep_values = [p for p in parts[1:]]
                data_start = i + 1
                break
            except ValueError:
                continue

    if sweep_values:
        # Column format: each data row has freq + N values
        result: dict[str, list[tuple[float, float]]] = {
            sv: [] for sv in sweep_values
        }
        for line in lines[data_start:]:
            parts = line.split()
            if not parts:
                continue
            try:
                freq = float(parts[0])
            except ValueError:
                continue
            for j, sv in enumerate(sweep_values):
                if j + 1 < len(parts):
                    try:
                        result[sv].append((freq, float(parts[j + 1])))
                    except ValueError:
                        pass
        return result

    # --- Fallback: Set format ---
    result = {}
    blocks = re.split(r"# Set No\.\s*\d+\s*\n", text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        m = re.match(r"\((\w+)\s*=\s*([\d.eE+-]+)\)", block)
        label = m.group(2) if m else "unknown"
        pairs: list[tuple[float, float]] = []
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("(") or line.startswith("freq"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    pairs.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    continue
        if pairs:
            result[label] = pairs
    return result


# ---------------------------------------------------------------------------
# 1. Create schematic
# ---------------------------------------------------------------------------

def create_schematic(client: VirtuosoClient) -> None:
    """Create an RC low-pass filter schematic.

    Circuit::

        V0(+) ---[R0 1k]--- OUT ---+
                                    |
        V0(-)                     [C0 c_val]
          |                         |
         GND0                     GND1

    V0 is a vdc source with AC magnitude = 1.
    C0 uses the design variable ``c_val`` so Maestro can sweep it.
    """
    print("[schematic] Creating RC filter...")

    with client.schematic.edit(LIB, CELL) as sch:
        sch.add_instance("analogLib", "vdc", (0, 0), name="V0")
        sch.add_instance("analogLib", "gnd", (0, -0.625), name="GND0")
        sch.add_instance("analogLib", "res", (1.5, 0.5), orientation="R90", name="R0")
        sch.add_instance("analogLib", "cap", (3.0, 0), name="C0")
        sch.add_instance("analogLib", "gnd", (3.0, -0.625), name="GND1")
        sch.add_wire_between_instance_terms("V0", "PLUS", "R0", "PLUS")
        sch.add_wire_between_instance_terms("R0", "MINUS", "C0", "PLUS")
        sch.add_wire_between_instance_terms("C0", "MINUS", "GND1", "gnd!")
        sch.add_wire_between_instance_terms("V0", "MINUS", "GND0", "gnd!")
        sch.add_pin_to_instance_term("C0", "PLUS", "OUT")

    # Set CDF parameters (Python API doesn't support this directly)
    cv = "_rcfCv"
    skill(client, f'{cv} = dbOpenCellViewByType("{LIB}" "{CELL}" "schematic" nil "a")')
    set_cdf_param(client, cv, "V0", "vdc", "0")
    set_cdf_param(client, cv, "V0", "acm", "1")    # AC magnitude = 1
    set_cdf_param(client, cv, "R0", "r", "1k")
    set_cdf_param(client, cv, "C0", "c", "c_val")   # design variable for sweep

    # Check & save (required before simulation!)
    skill(client, f"schCheck({cv})")
    skill(client, f"dbSave({cv})")

    # Open schematic in GUI (Maestro needs it visible)
    client.open_window(LIB, CELL, view="schematic")

    r = skill(client, f"{cv}~>instances~>name")
    print(f"[schematic] Instances: {r.output}")
    print("[schematic] Params: V0.acm=1, R0.r=1k, C0.c=c_val (design variable)")


# ---------------------------------------------------------------------------
# 2. Create Maestro view
# ---------------------------------------------------------------------------

def create_maestro(client: VirtuosoClient) -> str:
    """Create a Maestro view with AC analysis and return the session string.

    Key API sequence::

        maeOpenSetup -> maeCreateTest -> maeSetAnalysis -> maeAddOutput
        -> maeSetVar -> maeSaveSetup

    Notes:
    - ``?session`` must be a SKILL *string* (e.g. ``"fnxSession4"``).
    - Comma-separated values in ``maeSetVar`` create a parametric sweep.
    - ``maeSetAnalysis("tran" ?enable nil)`` disables the default tran.
    """
    print("[maestro] Creating Maestro view...")

    r = skill(client, f'maeOpenSetup("{LIB}" "{CELL}" "maestro")')
    ses = r.output.strip('"')
    print(f"[maestro] Session: {ses}")

    # Create test pointing to the schematic
    skill(client,
          f'maeCreateTest("AC" ?lib "{LIB}" ?cell "{CELL}" '
          f'?view "schematic" ?simulator "spectre" ?session "{ses}")')

    # Disable default tran, enable AC (1 Hz to 10 GHz, 20 pts/decade)
    skill(client, f'maeSetAnalysis("AC" "tran" ?enable nil ?session "{ses}")')
    skill(client,
          f'maeSetAnalysis("AC" "ac" ?enable t '
          f'?options `(("start" "1") ("stop" "10G") '
          f'("incrType" "Logarithmic") ("stepTypeLog" "Points Per Decade") '
          f'("dec" "20")) ?session "{ses}")')

    # Add output signal
    skill(client,
          f'maeAddOutput("Vout" "AC" ?outputType "net" '
          f'?signalName "/OUT" ?session "{ses}")')

    # Set design variable with comma-separated sweep values
    sweep_str = ",".join(C_VALUES)
    skill(client, f'maeSetVar("c_val" "{sweep_str}" ?session "{ses}")')

    # Save
    skill(client,
          f'maeSaveSetup(?lib "{LIB}" ?cell "{CELL}" '
          f'?view "maestro" ?session "{ses}")')

    print(f"[maestro] AC: 1 Hz - 10 GHz, 20 pts/dec | sweep c_val: {sweep_str}")
    return ses


# ---------------------------------------------------------------------------
# 3. Run simulation
# ---------------------------------------------------------------------------

def run_simulation(client: VirtuosoClient, ses: str) -> str:
    """Run simulation and return the results directory.

    The parametric sweep runs all C values in a single simulation.
    """
    print("[sim] Running AC simulation...")

    # Synchronous run: ?waitUntilDone t blocks until simulation finishes
    elapsed, r = timed_call(
        lambda: skill(client,
                      f'maeRunSimulation(?waitUntilDone t ?session "{ses}")',
                      timeout=300)
    )
    run_name = r.output.strip('"') if r.output else ""
    if not run_name or "Interactive" not in run_name:
        raise RuntimeError(f"Simulation failed to start: {r.output} {r.errors}")

    # Get results directory
    r = skill(client, "asiGetResultsDir(asiGetCurrentSession())")
    results_dir = r.output.strip('"')

    # .tmpADEDir means results are under a numbered Interactive.N directory
    if ".tmpADEDir" in results_dir:
        base = results_dir.split(".tmpADEDir")[0]
        r = client.run_shell_command(
            f"ls -1d {base}Interactive.*/psf/AC 2>/dev/null | tail -1"
        )
        alt = r.output.strip() if r.output else ""
        if alt and "Interactive" in alt:
            results_dir = alt

    print(f"[sim] {run_name} complete  [{format_elapsed(elapsed)}]")
    print(f"[sim] Results: {results_dir}")
    return results_dir


# ---------------------------------------------------------------------------
# 4. Read results
# ---------------------------------------------------------------------------

def read_results(client: VirtuosoClient,
                 results_dir: str) -> dict[str, list[tuple[float, float]]]:
    """Read AC magnitude via OCEAN and return per-sweep-value data."""
    remote_file = f"{REMOTE_TMP}_db.txt"
    local_file = Path("output/rc_filter_sweep_db.txt")
    local_file.parent.mkdir(parents=True, exist_ok=True)

    skill(client, f'openResults("{results_dir}")')
    skill(client, 'selectResults("ac")')

    r = skill(client, "outputs()")
    print(f"[read] Outputs: {r.output}")

    r = skill(client, "sweepNames()")
    print(f"[read] Sweeps: {r.output}")

    # Export dB20(mag(V(/OUT))) — includes all sweep sets
    skill(client,
          f'ocnPrint(dB20(mag(v("/OUT"))) '
          f'?numberNotation (quote scientific) ?numSpaces 1 '
          f'?output "{remote_file}")')
    client.download_file(remote_file, str(local_file))

    text = local_file.read_text()
    datasets = parse_ocnprint_sets(text)
    for label, data in datasets.items():
        print(f"[read] c_val={label}: {len(data)} points")
    print(f"[read] Saved to {local_file}")
    return datasets


# ---------------------------------------------------------------------------
# 5. Compare
# ---------------------------------------------------------------------------

def compare_results(datasets: dict[str, list[tuple[float, float]]]) -> None:
    """Print a comparison table and estimate -3 dB frequencies."""
    freqs_of_interest = [1e6, 1e7, 1e8, 1e9, 1e10]

    # Header
    labels = list(datasets.keys())
    header = "freq (Hz)".ljust(14)
    for label in labels:
        header += f"c_val={label}".rjust(16)
    print(f"\n{'=' * len(header)}")
    print(header)
    print(f"{'=' * len(header)}")

    for target_freq in freqs_of_interest:
        line = f"{target_freq:<14.2e}"
        for label in labels:
            data = datasets[label]
            closest = min(data, key=lambda p: abs(p[0] - target_freq))
            line += f"{closest[1]:>13.2f} dB"
        print(line)

    # Estimate -3 dB frequency
    print(f"\n--- Estimated -3 dB frequency ---")
    for label in labels:
        data = datasets[label]
        for i, (f, db) in enumerate(data):
            if db <= -3.0:
                if i > 0:
                    f_prev, db_prev = data[i - 1]
                    ratio = (-3.0 - db_prev) / (db - db_prev)
                    f_3db = f_prev + ratio * (f - f_prev)
                else:
                    f_3db = f
                print(f"  c_val={label}:  f_3dB = {f_3db:.3e} Hz")
                break
        else:
            print(f"  c_val={label}:  -3 dB not reached in range")


# ---------------------------------------------------------------------------
# 6. Open Maestro GUI with history results
# ---------------------------------------------------------------------------

def open_maestro_with_history(client: VirtuosoClient,
                              results_dir: str) -> None:
    """Open the Maestro GUI and display the latest simulation history.

    Steps:
    1. Close all existing mae sessions (edit mode is exclusive).
    2. List available histories from the simulation results directory.
    3. Open maestro window, make editable, restore history, save.
    """
    # Close all existing sessions
    r = skill(client, "maeGetSessions()")
    if r.output and r.output.strip() not in ("nil", ""):
        for ses in r.output.strip("()").replace('"', "").split():
            if ses:
                skill(client,
                      f'maeCloseSession(?session "{ses}" ?forceClose t)')

    # Find history names from results directory
    m = re.match(r"(.*/maestro/results/maestro/)", results_dir)
    if not m:
        print("[open] Cannot determine results base directory")
        return
    base = m.group(1)
    r = skill(client, f'getDirFiles("{base}")')
    dirs = r.output.strip("()").replace('"', "").split() if r.output else []
    histories = sorted([d for d in dirs if not d.startswith(".")])
    if not histories:
        print("[open] No histories found")
        return
    latest = histories[-1]
    print(f"[open] Available histories: {histories}")
    print(f"[open] Opening: {latest}")

    # Open GUI → editable → restore → save
    skill(client,
          f'deOpenCellView("{LIB}" "{CELL}" "maestro" "maestro" nil "r")')
    skill(client, "maeMakeEditable()")
    skill(client, f'maeRestoreHistory("{latest}")')
    skill(client,
          f'maeSaveSetup(?lib "{LIB}" ?cell "{CELL}" ?view "maestro")')
    print(f"[open] Maestro opened with {latest}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    client = VirtuosoClient.from_env()

    # 1. Create schematic
    create_schematic(client)

    # 2. Create Maestro with AC + sweep
    ses = create_maestro(client)

    # 3. Run (single run, sweep handles both C values)
    results_dir = run_simulation(client, ses)

    # 4. Read results
    datasets = read_results(client, results_dir)

    # 5. Compare
    if datasets:
        compare_results(datasets)
    else:
        print("[warn] No result sets found — check simulation output")
        return 1

    # 6. Open Maestro GUI with latest history
    open_maestro_with_history(client, results_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

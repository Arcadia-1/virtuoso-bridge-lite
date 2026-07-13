"""Maestro simulation runner — execute + read results.

Runs Maestro simulation in background mode (no GUI window),
waits for completion, then reads structured results.

Background mode is automation-safe: no modal dialogs can block
the SKILL channel during simulation.
"""

from __future__ import annotations

import json
import re

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import (
    open_session,
    close_session,
    run_and_wait,
    read_results,
    create_netlist_for_corner,
)
from sim_io.maestro.reader import fix_maestro_results


# ── Result Data Structure ──────────────────────────────────────

@dataclass
class MaestroSimResult:
    """Structured result from a Maestro simulation run."""
    lib: str
    tb_cell: str
    test_name: str
    history: str = ""
    sim_ok: bool = False
    overall_spec: Optional[str] = None
    overall_yield: Optional[str] = None
    points: list[dict] = field(default_factory=list)
    waveform_paths: list[str] = field(default_factory=list)
    run_dir: Optional[str] = None


# ── Simulation Runner ─────────────────────────────────────────

def run_maestro_sim(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    *,
    test_name: str = "",
    timeout: int = 600,
    export_waves: bool = True,
    wave_signals: list[str] | None = None,
    wave_analysis: str = "tran",
    run_dir: Path | None = None,
) -> MaestroSimResult:
    """Run Maestro simulation and read results in background mode.

    Opens a background session, runs simulation with non-blocking
    callback polling, reads structured results, optionally exports
    waveforms, then closes the session.

    Parameters
    ----------
    client : VirtuosoClient
    lib, tb_cell : Library and testbench cell names
    test_name : Maestro test name (default: tb_cell + "_test")
    timeout : Maximum wait time for simulation (seconds)
    export_waves : if True, export waveforms for key signals
    wave_signals : list of signal paths to export (e.g. ["/VOUT"])
    wave_analysis : analysis type for waveform export (default: "tran")
    run_dir : output directory for saving results
    """
    tname = test_name or f"{tb_cell}_test"
    result = MaestroSimResult(lib=lib, tb_cell=tb_cell, test_name=tname)

    print(f"\n{'='*60}")
    print(f" Maestro Sim: {lib}/{tb_cell}")
    print(f" Test: {tname}")
    print(f" Timeout: {timeout}s")
    print(f"{'='*60}\n")

    # Step 1: Open background session
    session = open_session(client, lib, tb_cell)
    print(f"[maestro-sim] Session: {session} (background)")

    try:
        # Step 1b: Pre-create netlist so Maestro won't pop "Update and Run" dialog.
        # On first run after TB creation, Maestro detects the netlist is stale
        # and shows a blocking confirmation dialog that hangs the SKILL channel.
        # Generating the netlist first avoids this.
        try:
            print(f"[maestro-sim] Pre-creating netlist to avoid dialog...")
            netlist_dir = f"/tmp/vb_maestro_netlist_{tname}"
            create_netlist_for_corner(
                client, tname, "tt", netlist_dir
            )
            print(f"[maestro-sim] Netlist created: {netlist_dir}")
        except Exception as e:
            print(f"[maestro-sim] NOTE: Pre-netlist failed (non-fatal): {e}")

        # Step 2: Run simulation + wait for completion
        print(f"[maestro-sim] Starting simulation...")
        history, status = run_and_wait(
            client, session=session, timeout=timeout
        )
        history_name = history.strip().strip('"') if history else ""
        result.history = history_name
        maestro_job_ok = status == "done"
        print(f"[maestro-sim] Maestro job {'completed' if maestro_job_ok else 'FAILED'}: "
              f"{history_name} (status={status})")

        if not maestro_job_ok:
            result.sim_ok = False
            return result

        # Step 3: Read structured results (per-point × per-output)
        print(f"[maestro-sim] Reading results...")
        results = read_results(client, session, lib=lib, cell=tb_cell,
                               include_raw=True)
        # Apply 7-col CSV fix if needed (upstream parser doesn't handle
        # the "Nominal Spec" column in single-run mode).
        results = fix_maestro_results(results)
        result.overall_spec = results.get("overall_spec")
        result.overall_yield = results.get("overall_yield")
        result.points = results.get("points", [])

        # Save raw CSV to run_dir for debug if available
        raw_csv = results.get("raw_csv")
        if raw_csv and run_dir:
            csv_path = run_dir / "maestro_detail.csv"
            csv_path.write_text(raw_csv, encoding="utf-8")
            print(f"[maestro-sim] Raw CSV saved: {csv_path}")

        # Detect Spectre failure: Maestro job "done" ≠ Spectre converged.
        # If read_results returns empty points, Spectre likely errored.
        has_results = bool(result.points)
        if not has_results:
            print(f"[maestro-sim] WARNING: Maestro job completed but no result "
                  f"points — Spectre may have failed inside Maestro")
        result.sim_ok = maestro_job_ok and has_results
        print(f"[maestro-sim] Simulation {'OK' if result.sim_ok else 'FAILED'}: "
              f"job={'done' if maestro_job_ok else 'failed'}, "
              f"results={'present' if has_results else 'empty'}")

        if not result.sim_ok:
            # Try to extract Spectre error from the log
            _check_spectre_log(client, session, lib, tb_cell, history_name)
            return result

        # Print summary
        for pt in result.points:
            pn = pt.get("point", "?")
            params = pt.get("parameters", {}) or {}
            param_str = ", ".join(f"{k}={v}" for k, v in params.items())
            print(f"  Point {pn}" + (f"  ({param_str})" if param_str else ""))
            for out_name, info in (pt.get("outputs", {}) or {}).items():
                val = info.get("value", "")
                pf = info.get("pass_fail", "")
                tag = f" [{pf}]" if pf else ""
                print(f"    {out_name} = {val}{tag}")

        if result.overall_spec:
            print(f"  Overall spec: {result.overall_spec}")

        # Step 4: Export waveforms (optional)
        if export_waves and wave_signals and run_dir:
            _export_waveforms(
                client, session, lib, tb_cell,
                wave_signals, wave_analysis, history_name, run_dir, result,
            )

    except Exception as e:
        print(f"[maestro-sim] ERROR: {e}")
        result.sim_ok = False

    finally:
        # Step 5: Always close the session
        try:
            close_session(client, session)
            print(f"[maestro-sim] Session closed")
        except Exception as e:
            print(f"[maestro-sim] WARNING: close_session failed: {e}")

    _print_sim_summary(result)
    return result


def _find_psf_dir(
    client: VirtuosoClient,
    session: str,
    lib: str,
    cell: str,
    history: str,
) -> str:
    """Locate the psfascii PSF directory for the given Maestro history.

    Tries two strategies in order:
      1. Construct the canonical path from the library's read path
         (ddGetObj(lib)~>readPath / cell / maestro/results/maestro / history)
         then SSH-find the deepest psf/ subdir.
      2. Fall back to maeOpenResults(?session … ?history …) +
         asiGetResultsDir(asiGetSession(session)) — used when the lib
         read path is unavailable.
    Returns the psf/ directory string, or "" on failure.
    """
    tunnel = client._tunnel

    def _ssh_find_psf(base: str) -> str:
        if tunnel is None:
            return ""
        try:
            r = tunnel.run_command(
                f'find "{base}" -maxdepth 6 -name "psf" -type d 2>/dev/null | head -1',
                timeout=15,
            )
            return (r.stdout or "").strip()
        except Exception as e:
            print(f"[maestro-sim] PSF SSH find failed under {base}: {e}")
            return ""

    # Strategy 1: canonical lib path
    r = client.execute_skill(f'ddGetObj("{lib}")~>readPath', timeout=10)
    lib_path = (r.output or "").strip().strip('"')
    if lib_path and lib_path.lower() != "nil":
        base = f"{lib_path}/{cell}/maestro/results/maestro/{history}"
        psf = _ssh_find_psf(base)
        if psf:
            print(f"[maestro-sim] PSF dir (lib path): {psf}")
            return psf

    # Strategy 2: maeOpenResults + asiGetResultsDir for our specific session
    try:
        client.execute_skill(
            f'maeOpenResults(?session "{session}" ?history "{history}")', timeout=15
        )
        r2 = client.execute_skill(
            f'asiGetResultsDir(asiGetSession("{session}"))', timeout=10
        )
        results_dir = (r2.output or "").strip().strip('"')
        client.execute_skill('maeCloseResults()')

        if results_dir and results_dir.lower() != "nil":
            # openResults can work directly on the results dir if it IS the psf dir
            r_open = client.execute_skill(f'openResults("{results_dir}")', timeout=15)
            if r_open.output and r_open.output.strip() not in ("nil", ""):
                print(f"[maestro-sim] PSF dir (maeOpenResults direct): {results_dir}")
                return results_dir
            # Otherwise find psf/ subdir
            psf = _ssh_find_psf(results_dir)
            if psf:
                print(f"[maestro-sim] PSF dir (maeOpenResults+find): {psf}")
                return psf
    except Exception as e:
        print(f"[maestro-sim] PSF dir via maeOpenResults failed: {e}")

    print(f"[maestro-sim] WARNING: Could not locate PSF dir for {lib}/{cell} history={history}")
    return ""


def _export_waveforms(
    client: VirtuosoClient,
    session: str,
    lib: str,
    cell: str,
    signals: list[str],
    analysis: str,
    history: str,
    run_dir: Path,
    result: MaestroSimResult,
) -> None:
    """Export waveforms via OCEAN for specified signals.

    Bypasses export_waveform() from virtuoso-bridge-lite to avoid two
    known issues with background sessions:
      1. execute_skill sends raw SKILL — expressions like v(\"/NET\") are
         invalid (backslash-escaping only applies inside evalstring).
         The correct OCEAN syntax is v("/NET") with real double-quotes.
      2. asiGetCurrentSession() returns the GUI-focused ADE session, not
         our background session; for automated runs this is typically nil.
    Instead, we locate the PSF directory directly (lib read path + SSH
    find) and call OCEAN openResults/selectResults/ocnPrint ourselves.
    """
    waves_dir = run_dir / "maestro_waves"
    waves_dir.mkdir(parents=True, exist_ok=True)

    psf_dir = _find_psf_dir(client, session, lib, cell, history)
    if not psf_dir:
        print(f"[maestro-sim] Waveform export skipped — PSF directory not found")
        return

    # Open PSF once for all signals
    r_open = client.execute_skill(f'openResults("{psf_dir}")', timeout=15)
    if not r_open.output or r_open.output.strip() in ("nil", ""):
        print(f"[maestro-sim] WARNING: openResults({psf_dir!r}) returned nil — no waveforms")
        return
    client.execute_skill(f'selectResults("{analysis}")')

    for sig in signals:
        safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", sig).strip("_") or "sig"
        local_path = waves_dir / f"{safe_name}.txt"
        nonce = f"{int(time.time() * 1000)}_{safe_name}"
        remote_path = f"/tmp/vb_wave_{nonce}.txt"
        try:
            # sig is already in OCEAN path format, e.g. "/SCK" — pass as a
            # plain quoted string; no backslash-escaping needed here.
            client.execute_skill(
                f'ocnPrint(v("{sig}") '
                f'?numberNotation \'scientific ?numSpaces 1 '
                f'?output "{remote_path}")',
                timeout=30,
            )
            client.download_file(remote_path, str(local_path))
            client.execute_skill(f'deleteFile("{remote_path}")')
            if local_path.exists() and local_path.stat().st_size > 0:
                result.waveform_paths.append(str(local_path))
                print(f"[maestro-sim] Waveform: {sig} → {local_path.name}")
            else:
                print(f"[maestro-sim] WARNING: waveform empty/missing for {sig!r}")
        except Exception as e:
            print(f"[maestro-sim] WARNING: waveform export failed for {sig!r}: {e}")


def _print_sim_summary(result: MaestroSimResult) -> None:
    print(f"\n{'='*60}")
    print(f" Maestro Sim Summary")
    print(f"{'='*60}")
    print(f"  Cell:      {result.lib}/{result.tb_cell}")
    print(f"  Test:      {result.test_name}")
    print(f"  History:   {result.history}")
    print(f"  Status:    {'OK' if result.sim_ok else 'FAILED'}")
    print(f"  Points:    {len(result.points)}")
    if result.overall_spec:
        print(f"  Spec:      {result.overall_spec}")
    if result.waveform_paths:
        print(f"  Waveforms: {len(result.waveform_paths)} files")
    print(f"{'='*60}\n")


def _check_spectre_log(
    client: VirtuosoClient,
    session: str,
    lib: str,
    cell: str,
    history: str,
) -> None:
    """Attempt to read the Spectre log and print error summary.

    When Maestro job completes but Spectre fails inside, this reads
    the Spectre log to surface the actual error (e.g. SFE-23 undefined
    model) instead of silently reporting success.
    """
    try:
        # Find the Spectre log directory from the results path
        log_skill = (
            f'let((p) p = ddGetObj("{lib}")~>readPath '
            f'strcat(p "/{cell}/maestro/results/maestro/{history}/"'
            f' " Spectre/{history}/spectre.log"))'
        )
        r = client.execute_skill(log_skill, timeout=15)
        log_path = (r.output or "").strip().strip('"')
        if not log_path or log_path == "nil":
            # Try alternative: just grep the result dir for .log files
            return

        # Read first and last portions of the log for errors
        r = client.execute_skill(
            f'let((f lines) '
            f'f = infile("{log_path}") '
            f'lines = nil '
            f'when(f '
            f'  for(i 0 200 '
            f'    let((line) line = gets(line f) '
            f'    when(line lines = cons(line lines)))) '
            f'  closePort(f)) '
            f'nreverse(lines))',
            timeout=15,
        )
        log_head = r.output or ""
        errors = re.findall(r'(SFE-\d+.*|Error:.*|error:.*|FATAL.*)', log_head)
        if errors:
            print(f"[maestro-sim] Spectre errors detected:")
            for err in errors[:10]:
                print(f"  {err.strip()}")
    except Exception as e:
        # Best-effort — don't fail the whole flow if log reading fails
        print(f"[maestro-sim] NOTE: Could not read Spectre log: {e}")

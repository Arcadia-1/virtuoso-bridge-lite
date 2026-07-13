"""
Maestro result utilities -- one-stop read + parse + spec check.

Wraps bridge-lite's read_results() with smic180's CSV fix and
parse_maestro_measurements() into a single call.

Core principle: bridge-lite does the heavy lifting (CSV export from
Maestro, parsing), smic180 just wraps + adds IO-ring-specific logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro.reader.runs import (
    read_results as _bridge_read_results,
    export_waveform as _bridge_export_waveform,
)

from sim_io.maestro.reader import fix_maestro_results
from sim_io.maestro.results import parse_maestro_measurements
from sim_io.sim.spec_check import check_maestro_specs, check_pin_measurements
from sim_io.pin_types import PinInfo


def read_and_parse(
    client: VirtuosoClient,
    session: str,
    lib: str,
    tb_cell: str,
    pins: list[PinInfo],
    *,
    classifications: dict | None = None,
    vdd: float = 1.8,
    history: str = "",
    run_dir: Path | None = None,
    export_waves: bool = False,
    wave_signals: list[str] | None = None,
) -> dict:
    """One-stop: read Maestro results -> fix CSV -> parse -> spec check.

    Wraps the following chain:
      1. bridge-lite read_results(include_raw=True)
      2. smic180 fix_maestro_results() (7-col CSV fix)
      3. smic180 parse_maestro_measurements() (per-pin measurements)
      4. smic180 check_maestro_specs() (pass/fail aggregation)
      5. (optional) bridge-lite export_waveform() for key signals

    Returns a dict with:
      - raw_result:   bridge-lite read_results() output (fixed)
      - measurements: per-pin measurements from parse_maestro_measurements()
      - spec_check:   SpecCheckResult.summary() dict
      - waveforms:    {signal: local_path} if export_waves=True
    """
    from sim_io.maestro.run import MaestroSimResult

    # Step 1: Read from Maestro via bridge-lite
    raw_result = _bridge_read_results(
        client, session,
        lib=lib, cell=tb_cell,
        history=history,
        include_raw=True,
    )

    # Step 2: Fix 7-col CSV if needed
    fixed_result = fix_maestro_results(raw_result)

    # Step 3: Parse into per-pin measurements
    # Build a minimal MaestroSimResult for parse_maestro_measurements
    sim_ok = fixed_result.get("overall_spec") is not None
    mae_sim = MaestroSimResult(
        lib=lib,
        tb_cell=tb_cell,
        test_name="",
        history=fixed_result.get("history", ""),
        sim_ok=sim_ok,
        overall_spec=fixed_result.get("overall_spec"),
        points=fixed_result.get("points", []),
    )
    measurements = parse_maestro_measurements(
        mae_sim, pins,
        classifications=classifications,
        vdd=vdd,
    )

    # Step 4: Spec check
    maestro_spec = check_maestro_specs(fixed_result)
    pin_spec = check_pin_measurements(measurements, i_max=0.1, vdd=vdd)

    # Merge: prefer maestro spec (has Maestro's own evaluation),
    # supplement with pin-level checks
    spec_summary = maestro_spec.summary()
    if pin_spec.failed_outputs > 0 and maestro_spec.passed:
        # Pin-level checks caught something Maestro didn't
        spec_summary["pin_level_check"] = pin_spec.summary()

    # Step 5: Optional waveform export
    waveforms = {}
    if export_waves and wave_signals:
        if not run_dir:
            run_dir = Path(".")
        waves_dir = run_dir / "maestro_waves"
        waves_dir.mkdir(parents=True, exist_ok=True)
        for sig in wave_signals:
            local_path = waves_dir / f"{sig.lstrip('/').replace('/', '_')}.txt"
            try:
                _bridge_export_waveform(
                    client, session, sig, str(local_path),
                    lib=lib, cell=tb_cell,
                    history=fixed_result.get("history", ""),
                    analysis="tran",
                )
                waveforms[sig] = str(local_path)
                print(f"[result-utils] Exported waveform: {sig} -> {local_path}")
            except Exception as exc:
                print(f"[result-utils] WARNING: Failed to export {sig}: {exc}")

    return {
        "raw_result": fixed_result,
        "measurements": measurements,
        "spec_check": spec_summary,
        "waveforms": waveforms,
    }


def export_key_waveforms(
    client: VirtuosoClient,
    session: str,
    lib: str,
    tb_cell: str,
    signals: list[str],
    output_dir: Path,
    *,
    history: str = "",
    analysis: str = "tran",
) -> dict[str, str]:
    """Export multiple waveforms via bridge-lite's export_waveform().

    Returns {signal_name: local_file_path}.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = {}
    for sig in signals:
        safe_name = sig.lstrip("/").replace("/", "_")
        local_path = output_dir / f"{safe_name}.txt"
        try:
            _bridge_export_waveform(
                client, session, sig, str(local_path),
                lib=lib, cell=tb_cell,
                history=history,
                analysis=analysis,
            )
            paths[sig] = str(local_path)
        except Exception as exc:
            print(f"[result-utils] WARNING: Failed to export {sig}: {exc}")

    return paths

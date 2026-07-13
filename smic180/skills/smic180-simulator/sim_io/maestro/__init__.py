"""Maestro (ADE Assembler) integration for SIM-IO.

Builds a fully configured Maestro test from SimDeckConfig, runs
simulation, and reads results — all in background mode (no GUI window).

Public API:
    ensure_maestro_view       — bootstrap maestro cellview if it doesn't exist
    build_maestro_setup       — SimDeckConfig → Maestro test configuration
    run_maestro_sim           — run simulation + read results
    parse_maestro_measurements — MaestroSimResult → Python measurements dict
    plot_maestro_waves        — maestro_waves/*.txt → SVG plots
    read_and_parse            — one-stop: read → fix → parse → spec check
    export_key_waveforms      — batch waveform export via bridge-lite
"""

from sim_io.maestro.setup import (
    ensure_maestro_view,
    build_maestro_setup,
    teardown_maestro_setup,
    discover_io_model_file,
)
from sim_io.maestro.run import (
    run_maestro_sim,
    MaestroSimResult,
)
from sim_io.maestro.results import parse_maestro_measurements
from sim_io.maestro.waves import plot_maestro_waves
from sim_io.maestro.reader import fix_maestro_results
from sim_io.maestro.result_utils import read_and_parse, export_key_waveforms

__all__ = [
    "ensure_maestro_view",
    "build_maestro_setup",
    "teardown_maestro_setup",
    "discover_io_model_file",
    "run_maestro_sim",
    "MaestroSimResult",
    "parse_maestro_measurements",
    "plot_maestro_waves",
    "fix_maestro_results",
    "read_and_parse",
    "export_key_waveforms",
]
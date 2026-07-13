"""Injected direct-Spectre result gate for immutable design iterations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
from typing import Any, Protocol

from ..artifacts import ArtifactStore
from .diagnostics import DiagnosticError, diagnose_mos_operating_points


class SimulationError(ValueError):
    """Raised when a fresh Spectre iteration does not prove valid results."""


class SpectreRunner(Protocol):
    def run(self, deck: Path, run_dir: Path) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DirectSimulationResult:
    success: bool
    run_dir: Path
    measurements: dict[str, float]
    operating_points: dict[str, dict[str, Any]]
    diagnostics: dict[str, dict[str, Any]]
    sample_counts: dict[str, int]


class DirectSpectreBackend:
    def __init__(self, runner: SpectreRunner, required_measurements: tuple[str, ...], required_analyses: tuple[str, ...]) -> None:
        self.runner = runner
        self.required_measurements = tuple(required_measurements)
        self.required_analyses = tuple(required_analyses)

    def run(self, deck: str | Path, iterations_root: str | Path, index: int) -> DirectSimulationResult:
        deck_path = Path(deck)
        run_dir = Path(iterations_root) / f"{index:04d}"
        if run_dir.exists():
            raise SimulationError(f"iteration directory already exists: {run_dir}")
        result = self.runner.run(deck_path, run_dir)
        if int(result.get("exit_code", -1)) != 0:
            raise SimulationError(f"Spectre exit code is not zero: {result.get('exit_code')}")
        if not (run_dir / "spectre.out").is_file() or not (run_dir / "raw").is_dir():
            raise SimulationError("fresh Spectre log or raw directory is missing")
        measurements = result.get("measurements")
        if not isinstance(measurements, dict):
            raise SimulationError("measurements are missing")
        normalized_measurements: dict[str, float] = {}
        for name in self.required_measurements:
            if name not in measurements:
                raise SimulationError(f"missing measurement: {name}")
            value = measurements[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise SimulationError(f"measurement {name} must be finite")
            normalized_measurements[name] = float(value)
        counts = result.get("sample_counts")
        if not isinstance(counts, dict):
            raise SimulationError("analysis sample counts are missing")
        normalized_counts: dict[str, int] = {}
        for analysis in self.required_analyses:
            count = counts.get(analysis)
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise SimulationError(f"analysis {analysis} has no fresh samples")
            normalized_counts[analysis] = count
        operating_points = result.get("operating_points")
        if not isinstance(operating_points, dict) or not operating_points:
            raise SimulationError("MOS operating point data is missing")
        try:
            diagnostics = diagnose_mos_operating_points(operating_points)
        except DiagnosticError as exc:
            raise SimulationError(str(exc)) from exc
        store = ArtifactStore(run_dir)
        store.write_json(run_dir / "measurements.json", normalized_measurements)
        store.write_json(run_dir / "operating_points.json", operating_points)
        store.write_json(run_dir / "diagnosis.json", diagnostics)
        store.write_json(run_dir / "manifest.json", {"deck": str(deck_path.resolve()), "exit_code": 0, "sample_counts": normalized_counts})
        return DirectSimulationResult(True, run_dir, normalized_measurements, operating_points, diagnostics, normalized_counts)

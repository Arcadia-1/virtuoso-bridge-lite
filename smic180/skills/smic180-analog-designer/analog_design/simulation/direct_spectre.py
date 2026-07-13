"""Injected direct-Spectre result gate for immutable design iterations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
import re
from typing import Any, Callable, Protocol

from ..artifacts import ArtifactStore
from .diagnostics import DiagnosticError, diagnose_mos_operating_points


class SimulationError(ValueError):
    """Raised when a fresh Spectre iteration does not prove valid results."""


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SimulationError(f"{label} must be finite")
    return float(value)


def _finite_complex(value: Any, label: str) -> complex:
    try:
        result = complex(value)
    except (TypeError, ValueError) as exc:
        raise SimulationError(f"{label} must be finite") from exc
    if not math.isfinite(result.real) or not math.isfinite(result.imag):
        raise SimulationError(f"{label} must be finite")
    return result


def _unity_gain_frequency(frequencies: list[float], gains: list[float]) -> float:
    for index, gain in enumerate(gains):
        if gain == 1.0:
            return frequencies[index]
        if index and gains[index - 1] > 1.0 > gain:
            x1 = math.log10(frequencies[index - 1])
            x2 = math.log10(frequencies[index])
            y1 = math.log10(gains[index - 1])
            y2 = math.log10(gain)
            return 10.0 ** (x1 - y1 * (x2 - x1) / (y2 - y1))
    raise SimulationError("AC response has no unity-gain crossing")


def extract_spectre_result(
    data: dict[str, Any],
    *,
    dut_instance: str,
    transient_scope: str,
) -> dict[str, Any]:
    """Extract auditable nominal measurements from fresh Bridge PSF data."""

    ac_names = ("ac_freq", "ac_VINP", "ac_VINN", "ac_VOUT")
    if any(name not in data or not isinstance(data[name], list) or not data[name] for name in ac_names):
        raise SimulationError("AC waveforms are missing")
    lengths = {len(data[name]) for name in ac_names}
    if len(lengths) != 1:
        raise SimulationError("AC waveforms have inconsistent sample counts")
    frequencies = [_finite_number(value, "AC frequency") for value in data["ac_freq"]]
    if any(value <= 0 for value in frequencies):
        raise SimulationError("AC frequencies must be positive")
    gains: list[float] = []
    for index, (output, positive, negative) in enumerate(zip(data["ac_VOUT"], data["ac_VINP"], data["ac_VINN"])):
        differential = _finite_complex(positive, f"AC VINP[{index}]") - _finite_complex(negative, f"AC VINN[{index}]")
        if differential == 0:
            raise SimulationError("AC differential excitation must be nonzero")
        gain = abs(_finite_complex(output, f"AC VOUT[{index}]") / differential)
        if not math.isfinite(gain) or gain <= 0:
            raise SimulationError("AC gain must be finite and positive")
        gains.append(gain)

    measurements = {
        "gain": 20.0 * math.log10(gains[0]),
        "ugbw": _unity_gain_frequency(frequencies, gains),
    }
    measurement_scopes = {"gain": "open_loop_differential_ac", "ugbw": "open_loop_differential_ac"}

    if "dc_VOUT" in data:
        measurements["output_dc"] = _finite_number(data["dc_VOUT"], "DC output")
        measurement_scopes["output_dc"] = "dc_operating_point"
    if "dc_VDD_SRC:p" in data:
        supply_current = abs(_finite_number(data["dc_VDD_SRC:p"], "DC supply current"))
        measurements["supply_current"] = supply_current
        measurement_scopes["supply_current"] = "dc_operating_point"
        if "dc_VDD" in data:
            measurements["power"] = abs(_finite_number(data["dc_VDD"], "DC supply voltage")) * supply_current
            measurement_scopes["power"] = "dc_operating_point"

    times = data.get("time")
    output = data.get("VOUT")
    transient_count = 0
    if isinstance(times, list) and isinstance(output, list) and len(times) == len(output) and len(times) >= 2:
        normalized_times = [_finite_number(value, "transient time") for value in times]
        normalized_output = [_finite_number(value, "transient output") for value in output]
        slopes = [
            abs((normalized_output[index] - normalized_output[index - 1]) / (normalized_times[index] - normalized_times[index - 1]))
            for index in range(1, len(normalized_times))
            if normalized_times[index] > normalized_times[index - 1]
        ]
        if not slopes:
            raise SimulationError("transient waveform has no positive time steps")
        measurements["open_loop_slew_rate"] = max(slopes)
        measurement_scopes["open_loop_slew_rate"] = transient_scope
        transient_count = len(normalized_times)

    prefix = f"dc_{dut_instance}."
    fields: dict[str, dict[str, Any]] = {}
    for name, value in data.items():
        if not name.startswith(prefix) or ":" not in name:
            continue
        instance, field = name[len(prefix):].split(":", 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            continue
        fields.setdefault(instance, {})[field] = float(value)

    return {
        "measurements": measurements,
        "measurement_scopes": measurement_scopes,
        "operating_points": fields,
        "sample_counts": {"ac": len(frequencies), "tran": transient_count, "op": 1 if fields else 0},
    }


class SpectreRunner(Protocol):
    def run(self, deck: Path, run_dir: Path) -> dict[str, Any]: ...


class BridgeSpectreRunner:
    """Adapt the public Bridge Spectre result into the designer result contract."""

    def __init__(
        self,
        simulator_factory: Callable[[Path], Any],
        *,
        dut_instance: str = "X_DUT",
        transient_scope: str = "open_loop_differential_step",
    ) -> None:
        self.simulator_factory = simulator_factory
        self.dut_instance = dut_instance
        self.transient_scope = transient_scope

    def run(self, deck: Path, run_dir: Path) -> dict[str, Any]:
        run_dir.mkdir(parents=True, exist_ok=False)
        result = self.simulator_factory(run_dir).run_simulation(deck, {})
        returncode = int(getattr(result, "metadata", {}).get("returncode", -1))
        log_path = run_dir / "spectre.out"
        raw_dir = run_dir / f"{deck.stem}.raw"
        exit_code = 0 if bool(getattr(result, "ok", False)) and returncode == 0 else returncode if returncode >= 0 else 1
        if exit_code == 0 and log_path.is_file():
            match = re.search(r"spectre completes with\s+(\d+)\s+errors?", log_path.read_text(encoding="utf-8", errors="replace"), re.IGNORECASE)
            if match and int(match.group(1)) != 0:
                exit_code = 1
        parsed = extract_spectre_result(
            dict(getattr(result, "data", {})),
            dut_instance=self.dut_instance,
            transient_scope=self.transient_scope,
        ) if exit_code == 0 else {"measurements": {}, "measurement_scopes": {}, "operating_points": {}, "sample_counts": {}}
        return {
            "exit_code": exit_code,
            "raw_dir": str(raw_dir),
            **parsed,
            "backend_errors": list(getattr(result, "errors", ())),
            "backend_warnings": list(getattr(result, "warnings", ())),
        }


@dataclass(frozen=True)
class DirectSimulationResult:
    success: bool
    run_dir: Path
    measurements: dict[str, float]
    measurement_scopes: dict[str, str]
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
        raw_dir = Path(result.get("raw_dir", run_dir / "raw"))
        try:
            raw_dir.resolve().relative_to(run_dir.resolve())
        except ValueError as exc:
            raise SimulationError("fresh Spectre raw directory must stay inside the iteration directory") from exc
        if not (run_dir / "spectre.out").is_file() or not raw_dir.is_dir():
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
        scopes = result.get("measurement_scopes")
        if not isinstance(scopes, dict):
            raise SimulationError("measurement scopes are missing")
        normalized_scopes: dict[str, str] = {}
        for name in self.required_measurements:
            scope = scopes.get(name)
            if not isinstance(scope, str) or not scope:
                raise SimulationError(f"measurement {name} scope is missing")
            normalized_scopes[name] = scope
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
        store.write_json(run_dir / "measurement_scopes.json", normalized_scopes)
        store.write_json(run_dir / "operating_points.json", operating_points)
        store.write_json(run_dir / "diagnosis.json", diagnostics)
        store.write_json(run_dir / "manifest.json", {"deck": str(deck_path.resolve()), "exit_code": 0, "sample_counts": normalized_counts})
        return DirectSimulationResult(True, run_dir, normalized_measurements, normalized_scopes, operating_points, diagnostics, normalized_counts)

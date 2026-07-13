"""Semantic circuit and fresh metric equivalence gates."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from ..artifacts import ArtifactStore
from .spectre_reader import ParsedCircuit, parse_spectre_circuit


class EquivalenceError(ValueError):
    """Raised when equivalence evidence is incomplete or invalid."""


def _normalized_parameters(model: str, values: Mapping[str, float | str], defaults: Mapping[str, Mapping[str, float]]) -> dict[str, float | str]:
    result = dict(defaults.get(model, {}))
    result.update(values)
    return result


def _equal_parameter(left: float | str, right: float | str) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-15)
    return left == right


def compare_netlists(left_text: str, right_text: str, *, parameter_defaults: Mapping[str, Mapping[str, float]] | None = None) -> dict[str, Any]:
    defaults = parameter_defaults or {}
    left = parse_spectre_circuit(left_text)
    right = parse_spectre_circuit(right_text)
    differences: list[str] = []
    if left.name != right.name:
        differences.append(f"subcircuit name differs: {left.name} != {right.name}")
    if left.ports != right.ports:
        differences.append(f"ports differ: {left.ports} != {right.ports}")
    if set(left.instances) != set(right.instances):
        differences.append(f"instance sets differ: {sorted(left.instances)} != {sorted(right.instances)}")
    for name in sorted(set(left.instances) & set(right.instances)):
        first = left.instances[name]
        second = right.instances[name]
        if first.model != second.model:
            differences.append(f"{name} model differs: {first.model} != {second.model}")
        if first.nodes != second.nodes:
            differences.append(f"{name} nodes differ: {first.nodes} != {second.nodes}")
        first_params = _normalized_parameters(first.model, first.parameters, defaults)
        second_params = _normalized_parameters(second.model, second.parameters, defaults)
        if set(first_params) != set(second_params):
            differences.append(f"{name} parameter sets differ: {sorted(first_params)} != {sorted(second_params)}")
        for parameter in sorted(set(first_params) & set(second_params)):
            if not _equal_parameter(first_params[parameter], second_params[parameter]):
                differences.append(f"{name}.{parameter} differs: {first_params[parameter]} != {second_params[parameter]}")
    return {"equivalent": not differences, "differences": differences}


def compare_metrics(left: Mapping[str, object], right: Mapping[str, object], tolerances: Mapping[str, Mapping[str, float]], *, fresh: bool = True) -> dict[str, Any]:
    if not fresh:
        raise EquivalenceError("metric comparison requires fresh simulation results")
    comparisons: dict[str, dict[str, Any]] = {}
    for name, limits in tolerances.items():
        if name not in left or name not in right:
            raise EquivalenceError(f"metric is missing: {name}")
        first, second = left[name], right[name]
        if isinstance(first, bool) or isinstance(second, bool) or not isinstance(first, (int, float)) or not isinstance(second, (int, float)):
            raise EquivalenceError(f"metric must be numeric: {name}")
        if not math.isfinite(float(first)) or not math.isfinite(float(second)):
            raise EquivalenceError(f"metric must be finite: {name}")
        absolute = abs(float(first) - float(second))
        relative = absolute / max(abs(float(first)), abs(float(second)), 1e-30)
        passed = absolute <= float(limits.get("abs", 0.0)) or relative <= float(limits.get("rel", 0.0))
        comparisons[name] = {"left": float(first), "right": float(second), "absolute_error": absolute, "relative_error": relative, "passed": passed}
    return {"equivalent": all(item["passed"] for item in comparisons.values()), "comparisons": comparisons}


def write_equivalence_confirmation(directory: str | Path, structural: Mapping[str, Any], simulation: Mapping[str, Any]) -> Path:
    if not structural.get("equivalent") or not simulation.get("equivalent"):
        raise EquivalenceError("both structural and simulation equivalence must pass")
    root = Path(directory)
    store = ArtifactStore(root)
    structural_path = store.write_json(root / "structural_comparison.json", structural)
    simulation_path = store.write_json(root / "simulation_comparison.json", simulation)
    return store.confirm(root / "equivalence.confirmed.json", "equivalence_passed", [structural_path, simulation_path])

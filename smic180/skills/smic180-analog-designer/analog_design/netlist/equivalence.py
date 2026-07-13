"""Semantic circuit and fresh metric equivalence gates."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

from ..artifacts import ArtifactStore
from .spectre_reader import parse_spectre_circuit


class EquivalenceError(ValueError):
    """Raised when equivalence evidence is incomplete or invalid."""


def _normalized_parameters(
    model: str,
    values: Mapping[str, float | str],
    defaults: Mapping[str, Mapping[str, float]],
    ignored: Mapping[str, set[str]],
) -> dict[str, float | str]:
    result = dict(defaults.get(model, {}))
    result.update(values)
    for name in ignored.get(model, set()):
        result.pop(name, None)
    return result


def _equal_parameter(left: float | str, right: float | str, limits: Mapping[str, float]) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(
            float(left),
            float(right),
            rel_tol=float(limits.get("rel", 1e-9)),
            abs_tol=float(limits.get("abs", 1e-15)),
        )
    return left == right


def compare_netlists(
    left_text: str,
    right_text: str,
    *,
    parameter_defaults: Mapping[str, Mapping[str, float]] | None = None,
    ignored_parameters: Mapping[str, set[str]] | None = None,
    parameter_tolerances: Mapping[str, Mapping[str, float]] | None = None,
    right_flat_name: str | None = None,
    right_flat_ports: tuple[str, ...] = (),
) -> dict[str, Any]:
    defaults = parameter_defaults or {}
    ignored = ignored_parameters or {}
    tolerances = parameter_tolerances or {}
    left = parse_spectre_circuit(left_text)
    right = parse_spectre_circuit(right_text, flat_name=right_flat_name, flat_ports=right_flat_ports)
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
        first_params = _normalized_parameters(first.model, first.parameters, defaults, ignored)
        second_params = _normalized_parameters(second.model, second.parameters, defaults, ignored)
        if set(first_params) != set(second_params):
            differences.append(f"{name} parameter sets differ: {sorted(first_params)} != {sorted(second_params)}")
        for parameter in sorted(set(first_params) & set(second_params)):
            limits = tolerances.get(f"{name}.{parameter}", tolerances.get(parameter, {}))
            if not _equal_parameter(first_params[parameter], second_params[parameter], limits):
                differences.append(f"{name}.{parameter} differs: {first_params[parameter]} != {second_params[parameter]}")
    return {"equivalent": not differences, "differences": differences}


def build_virtuoso_replay_deck(direct_deck: str, exported_body: str) -> str:
    """Replace only the direct DUT body with a fresh flat Virtuoso export."""

    direct_lines = direct_deck.splitlines()
    start = next((index for index, line in enumerate(direct_lines) if line.strip().startswith("subckt ")), None)
    if start is None:
        raise EquivalenceError("direct deck contains no DUT subcircuit")
    end = next((index for index in range(start + 1, len(direct_lines)) if direct_lines[index].strip().startswith("ends")), None)
    if end is None:
        raise EquivalenceError("direct DUT subcircuit is not terminated")
    exported_lines = exported_body.strip().splitlines()
    if any(line.strip().startswith(("subckt ", "ends")) for line in exported_lines):
        raise EquivalenceError("Virtuoso replay requires a flat exported DUT body")
    replay_lines = [
        *direct_lines[: start + 1],
        *exported_lines,
        direct_lines[end],
        *direct_lines[end + 1 :],
    ]
    return "\n".join(replay_lines) + "\n"

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
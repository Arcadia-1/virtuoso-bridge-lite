"""Post-publication Spectre verification for a confirmed analog result cell.

This module is intentionally separate from the optimization state machine.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class FinalValidationError(RuntimeError):
    """Raised when final-result verification cannot be trusted."""


@dataclass(frozen=True)
class PublishedContext:
    run_dir: Path
    config_path: Path
    library: str
    source_cell: str
    work_cell: str
    result_cell: str
    baseline_testbench: str
    final_testbench: str
    dut_instance: str
    candidate_hash: str
    parameters: Mapping[str, Any]
    config: Mapping[str, Any]


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalValidationError(f"{label} is missing or invalid: {path}") from exc
    if not isinstance(value, Mapping):
        raise FinalValidationError(f"{label} must be a JSON object")
    return value


def load_published_context(run_dir: str | Path) -> PublishedContext:
    root = Path(run_dir).resolve()
    if not root.is_dir():
        raise FinalValidationError(f"run directory does not exist: {root}")
    state = _read_json(root / "workflow_state.json", "workflow state")
    if state.get("state") not in {"published", "reported"}:
        raise FinalValidationError("result cell is not in a published workflow state")
    intent = _read_json(root / "publication.json", "publication intent")
    confirmed = _read_json(root / "publication.confirmed.json", "publication confirmation")
    candidate_hash = intent.get("candidate_hash")
    parameters = intent.get("parameters")
    if not isinstance(parameters, Mapping):
        raise FinalValidationError("publication parameters are missing")
    if not isinstance(candidate_hash, str) or confirmed.get("candidate_hash") != candidate_hash:
        raise FinalValidationError("published result confirmation hash does not match")
    config_path = root / "analog_opt_config.resolved.json"
    config = _read_json(config_path, "resolved configuration")
    design = config.get("design")
    if not isinstance(design, Mapping):
        raise FinalValidationError("resolved configuration design is missing")
    required = ("library", "cell", "work_cell", "result_cell", "testbench_cell")
    if any(not isinstance(design.get(name), str) or not design.get(name) for name in required):
        raise FinalValidationError("resolved design identifiers are incomplete")
    dut_instance = design.get("dut_instance", "DUT")
    if not isinstance(dut_instance, str) or not dut_instance:
        raise FinalValidationError("DUT instance identifier is invalid")
    final_testbench = design["result_cell"] + "_tb"
    names = {design["cell"], design["work_cell"], design["result_cell"], final_testbench}
    if len(names) != 4:
        raise FinalValidationError("final testbench must be distinct from source, work, and result cells")
    return PublishedContext(root, config_path, design["library"], design["cell"], design["work_cell"],
                            design["result_cell"], design["testbench_cell"], final_testbench,
                            dut_instance, candidate_hash, dict(parameters), config)


def verify_netlist_text(text: str, result_cell: str, work_cell: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise FinalValidationError("final netlist is empty")
    if result_cell not in text:
        raise FinalValidationError("final netlist does not reference the result cell")
    if work_cell and work_cell in text:
        raise FinalValidationError("final netlist still references the work cell")


def write_confirmation(run_dir: str | Path, checks: Mapping[str, Any], details: Mapping[str, Any]) -> Path:
    required = ("result_exists", "final_tb_exists", "dut_uses_result", "netlist_uses_result",
                "spectre_passed", "pvt_passed", "fresh_results")
    if any(checks.get(name) is not True for name in required):
        raise FinalValidationError("final validation checks are incomplete")
    root = Path(run_dir)
    target = root / "final_validation" / "final_validation.confirmed.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "status": "passed", "checks": dict(checks), "details": dict(details)}
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
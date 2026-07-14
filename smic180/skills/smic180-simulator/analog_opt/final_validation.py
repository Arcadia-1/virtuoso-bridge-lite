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
    profile_summary_hash: str | None = None


@dataclass(frozen=True)
class FinalProfilePlan:
    profile_id: str
    role: str
    baseline_testbench: str
    final_testbench: str
    dut_instance: str


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
    profile_summary_hash = intent.get("profile_summary_hash")
    if profile_summary_hash is not None:
        if not isinstance(profile_summary_hash, str) or len(profile_summary_hash) != 64 or confirmed.get("profile_summary_hash") != profile_summary_hash:
            raise FinalValidationError("published profile summary hash does not match")
    elif confirmed.get("profile_summary_hash") is not None:
        raise FinalValidationError("published profile summary hash does not match")
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
                            dut_instance, candidate_hash, dict(parameters), config, profile_summary_hash)


def verify_netlist_text(text: str, result_cell: str, work_cell: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise FinalValidationError("final netlist is empty")
    if result_cell not in text:
        raise FinalValidationError("final netlist does not reference the result cell")
    if work_cell and work_cell in text:
        raise FinalValidationError("final netlist still references the work cell")


def build_final_profile_plan(context: PublishedContext) -> tuple[FinalProfilePlan, ...]:
    raw_profiles = context.config.get("verification_profiles")
    if not isinstance(raw_profiles, (list, tuple)) or not raw_profiles:
        return (FinalProfilePlan("default", "legacy", context.baseline_testbench,
                                 context.final_testbench, context.dut_instance),)
    if len(raw_profiles) == 1 and isinstance(raw_profiles[0], Mapping) and raw_profiles[0].get("id") == "default" and raw_profiles[0].get("role") == "legacy":
        return (FinalProfilePlan("default", "legacy", context.baseline_testbench,
                                 context.final_testbench, context.dut_instance),)
    plans = []
    baseline_cells = {raw.get("testbench_cell") for raw in raw_profiles if isinstance(raw, Mapping) and isinstance(raw.get("testbench_cell"), str)}
    forbidden = {context.source_cell, context.work_cell, context.result_cell, *baseline_cells}
    for raw in raw_profiles:
        if not isinstance(raw, Mapping):
            raise FinalValidationError("verification profile must be a mapping")
        profile_id = raw.get("id")
        baseline = raw.get("testbench_cell")
        dut_instance = raw.get("dut_instance")
        role = raw.get("role")
        if any(not isinstance(value, str) or not value for value in (profile_id, baseline, dut_instance, role)):
            raise FinalValidationError("verification profile identifiers are incomplete")
        final_testbench = context.result_cell + "_" + profile_id + "_tb"
        if final_testbench in forbidden:
            raise FinalValidationError("final profile testbench is not isolated: " + profile_id)
        plans.append(FinalProfilePlan(profile_id, role, baseline, final_testbench, dut_instance))
    if len({plan.profile_id for plan in plans}) != len(plans) or len({plan.final_testbench for plan in plans}) != len(plans):
        raise FinalValidationError("final profile identifiers must be unique")
    return tuple(plans)


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


def write_profile_confirmation(run_dir: str | Path, checks: Mapping[str, Any], details: Mapping[str, Any]) -> Path:
    required_checks = ("result_exists", "final_tb_exists", "dut_uses_result", "netlist_uses_result",
                       "spectre_passed", "pvt_passed", "fresh_results")
    required_profiles = details.get("required_profile_ids") if isinstance(details, Mapping) else None
    if not isinstance(required_profiles, (list, tuple)) or not required_profiles or len(set(required_profiles)) != len(required_profiles):
        raise FinalValidationError("required final validation profiles are invalid")
    if not isinstance(checks, Mapping):
        raise FinalValidationError("profile validation checks must be a mapping")
    normalized = {}
    for profile_id in required_profiles:
        profile_checks = checks.get(profile_id)
        if not isinstance(profile_checks, Mapping):
            raise FinalValidationError("final validation profile is missing: " + str(profile_id))
        if any(profile_checks.get(name) is not True for name in required_checks):
            raise FinalValidationError("final validation checks are incomplete for " + str(profile_id))
        normalized[str(profile_id)] = dict(profile_checks)
    root = Path(run_dir)
    target = root / "final_validation" / "final_validation.confirmed.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "status": "passed", "profiles": normalized, "details": dict(details)}
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target

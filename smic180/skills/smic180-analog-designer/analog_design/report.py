"""Truthful machine-readable and engineering-readable design reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .workflow import _STATES, WorkflowState


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _operating_point_devices(raw_metrics: dict[str, Any]) -> dict[str, dict[str, float]]:
    devices: dict[str, dict[str, float]] = {}
    for name, value in raw_metrics.items():
        if not _is_number(value) or not name.startswith("op."):
            continue
        parts = name.split(".", 2)
        if len(parts) != 3:
            continue
        devices.setdefault(parts[1], {})[parts[2]] = float(value)
    return devices


def _optimizer_context(root: Path) -> tuple[dict[str, Any], Path | None]:
    reference = _read_json(root / "optimizer" / "run_reference.json", {})
    workflow_path = reference.get("workflow_state") if isinstance(reference, dict) else None
    if not isinstance(workflow_path, str):
        return {}, None
    optimizer_root = Path(workflow_path).parent
    workflow = _read_json(Path(workflow_path), {})
    raw_metrics = workflow.get("best", {}).get("metrics", {})
    if not isinstance(raw_metrics, dict):
        raw_metrics = {}
    metrics = {
        name: value for name, value in raw_metrics.items()
        if _is_number(value) and (name.startswith(("ac.", "stb.", "tran.closed_loop_slew.")) or "power" in name or "supply_current" in name)
    }
    devices = _operating_point_devices(raw_metrics)
    saturation = {
        name: values["saturation_margin"] for name, values in devices.items()
        if _is_number(values.get("saturation_margin"))
    }
    candidate_hash = reference.get("candidate_hash")
    if candidate_hash != workflow.get("candidate_hash"):
        candidate_hash = None
    profile_summary_hash = (
        reference.get("profile_summary_hash")
        if reference.get("profile_summary_hash") == workflow.get("profile_summary_hash")
        else None
    )
    return {
        "candidate_hash": candidate_hash,
        "state": workflow.get("state"),
        "profile_summary_hash": profile_summary_hash,
        "parameters": workflow.get("best", {}).get("parameters", {}),
        "metrics": metrics,
        "operating_point": {
            "minimum_saturation_margin": min(saturation.values()) if saturation else None,
            "saturation_margins": saturation,
            "devices": devices,
        },
    }, optimizer_root


def _optimization_history(optimizer_root: Path | None) -> dict[str, Any]:
    if optimizer_root is None:
        return {"evaluation_count": 0, "success_count": 0, "failure_count": 0, "best_objective": None}
    raw = _read_json(optimizer_root / "search_history.json", {})
    history = raw.get("history", []) if isinstance(raw, dict) else []
    if not isinstance(history, list):
        history = []
    objectives = [float(item["objective"]) for item in history if isinstance(item, dict) and _is_number(item.get("objective"))]
    success_count = sum(1 for item in history if isinstance(item, dict) and item.get("success") is True)
    result = _read_json(optimizer_root / "result_manifest.json", {})
    result_failures = result.get("failures", []) if isinstance(result, dict) else []
    return {
        "evaluation_count": len(history),
        "success_count": success_count,
        "failure_count": max(len(history) - success_count, len(result_failures) if isinstance(result_failures, list) else 0),
        "best_objective": min(objectives) if objectives else None,
        "publishable": result.get("publishable") if isinstance(result, dict) else None,
    }


def _pvt_metric_ranges(pvt_raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(pvt_raw, dict) or not isinstance(pvt_raw.get("points"), list):
        return {}
    values: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for point in pvt_raw["points"]:
        if not isinstance(point, dict) or not isinstance(point.get("metrics"), dict):
            continue
        condition = {
            "point_id": point.get("point_id"),
            "corner": point.get("corner"),
            "voltage": point.get("voltage"),
            "temperature_c": point.get("temperature_c", point.get("temperature")),
        }
        for name, value in point["metrics"].items():
            if _is_number(value) and (name.startswith("ac.") or "power" in name or "supply_current" in name):
                values.setdefault(name, []).append((float(value), condition))
    ranges: dict[str, dict[str, Any]] = {}
    for name, records in values.items():
        minimum = min(records, key=lambda item: item[0])
        maximum = max(records, key=lambda item: item[0])
        ranges[name] = {
            "minimum": minimum[0], "minimum_point": minimum[1].get("point_id"), "minimum_condition": minimum[1],
            "maximum": maximum[0], "maximum_point": maximum[1].get("point_id"), "maximum_condition": maximum[1],
        }
    return ranges




def _profile_verification_scope(
    optimizer_root: Path | None,
    optimizer: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    unverified = {
        "phase_margin": {
            "status": "unverified",
            "reason": "a matching STB profile confirmation chain is unavailable",
        },
        "closed_loop_slew_rate": {
            "status": "unverified",
            "reason": "a matching closed-loop slew profile confirmation chain is unavailable",
        },
    }
    risks = [
        "Phase margin remains unverified until the STB profile confirmation chain matches.",
        "Standard closed-loop slew rate remains unverified until the dedicated profile confirmation chain matches.",
    ]
    if optimizer_root is None:
        return unverified, risks
    candidate_hash = optimizer.get("candidate_hash")
    profile_hash = optimizer.get("profile_summary_hash")
    if (
        not isinstance(candidate_hash, str)
        or not candidate_hash
        or not isinstance(profile_hash, str)
        or len(profile_hash) != 64
    ):
        return unverified, risks
    required = ["open_loop", "stability", "closed_loop_slew"]
    final_value = _read_json(
        optimizer_root / "final_validation" / "final_validation.confirmed.json", {}
    )
    maestro_value = _read_json(
        optimizer_root / "maestro_validation" / "maestro_validation.confirmed.json", {}
    )
    stability = _read_json(optimizer_root / "stability.confirmed.json", {})
    slew = _read_json(optimizer_root / "closed_loop_slew.confirmed.json", {})
    final_details = final_value.get("details", {}) if isinstance(final_value, dict) else {}
    maestro_details = maestro_value.get("details", {}) if isinstance(maestro_value, dict) else {}
    confirmations = (("stability", stability), ("closed_loop_slew", slew))
    chain_matches = (
        final_value.get("version") == 2
        and final_value.get("status") == "passed"
        and final_details.get("candidate_hash") == candidate_hash
        and final_details.get("profile_summary_hash") == profile_hash
        and final_details.get("required_profile_ids") == required
        and maestro_value.get("version") == 2
        and maestro_value.get("status") == "passed"
        and maestro_details.get("profile_summary_hash") == profile_hash
        and maestro_details.get("required_profile_ids") == required
        and all(
            value.get("profile_id") == profile_id
            and value.get("candidate_hash") == candidate_hash
            and value.get("profile_summary_hash") == profile_hash
            for profile_id, value in confirmations
        )
    )
    if not chain_matches:
        return unverified, risks
    metrics = optimizer.get("metrics", {})
    phase = metrics.get("stb.stability.loop.phase_margin_deg") if isinstance(metrics, dict) else None
    rise = metrics.get("tran.closed_loop_slew.step.VOUT.slew_rise_v_per_s") if isinstance(metrics, dict) else None
    fall = metrics.get("tran.closed_loop_slew.step.VOUT.slew_fall_v_per_s") if isinstance(metrics, dict) else None
    if not all(_is_number(value) for value in (phase, rise, fall)):
        return unverified, risks
    return {
        "phase_margin": {"status": "verified", "value_deg": float(phase)},
        "closed_loop_slew_rate": {
            "status": "verified",
            "rise_v_per_s": float(rise),
            "fall_v_per_s": float(fall),
        },
    }, []

def write_report(run_dir: str | Path, *, output_dir: str | Path | None = None) -> tuple[Path, Path]:
    root = Path(run_dir)
    state = WorkflowState.load(root / "workflow_state.json")
    current_index = _STATES.index(state.current)
    stages = {name: ("confirmed" if index <= current_index else "unverified") for index, name in enumerate(_STATES)}
    optimizer, optimizer_root = _optimizer_context(root)
    verification_scope, residual_risks = _profile_verification_scope(optimizer_root, optimizer)
    pvt_raw = _read_json(optimizer_root / "pvt_results.json", {}) if optimizer_root else {}
    final_raw = _read_json(optimizer_root / "final_validation" / "final_validation.confirmed.json", {}) if optimizer_root else {}
    maestro_raw = _read_json(optimizer_root / "maestro_validation" / "maestro_validation.confirmed.json", {}) if optimizer_root else {}
    final_details = final_raw.get("details", {}) if isinstance(final_raw, dict) else {}
    maestro_details = maestro_raw.get("details", {}) if isinstance(maestro_raw, dict) else {}
    maestro_checks = maestro_raw.get("checks", {}) if isinstance(maestro_raw, dict) else {}

    report = {
        "version": 1,
        "status": "complete" if state.current == "final_validation_passed" else "incomplete",
        "current_state": state.current,
        "stages": stages,
        "specification": _read_json(root / "inputs" / "design_spec.json", {}),
        "topology": _read_json(root / "topology" / "topology_plan.json", {}),
        "initial_sizing": _read_json(root / "sizing" / "initial_sizing.json", {}),
        "windows_nominal": _read_json(root / "windows_sim" / "measurements.json", {}),
        "equivalence": {
            "structural": _read_json(root / "equivalence" / "structural_comparison.json", {}),
            "simulation": _read_json(root / "equivalence" / "simulation_comparison.json", {}),
        },
        "optimizer": optimizer,
        "optimization_history": _optimization_history(optimizer_root),
        "pvt": {
            "overall_passed": pvt_raw.get("overall_passed") if isinstance(pvt_raw, dict) else None,
            "point_count": len(pvt_raw.get("points", [])) if isinstance(pvt_raw, dict) else 0,
            "failure_count": len(pvt_raw.get("failures", [])) if isinstance(pvt_raw, dict) else 0,
            "worst_by_spec": pvt_raw.get("worst_by_spec", {}) if isinstance(pvt_raw, dict) else {},
            "metric_ranges": _pvt_metric_ranges(pvt_raw),
        },
        "publication": {
            "status": final_raw.get("status") if isinstance(final_raw, dict) else None,
            "library": final_details.get("library"),
            "result_cell": final_details.get("result_cell"),
            "final_testbench": final_details.get("final_testbench"),
            "candidate_hash": final_details.get("candidate_hash"),
        },
        "maestro": {
            "status": maestro_raw.get("status") if isinstance(maestro_raw, dict) else None,
            "history": maestro_details.get("history"),
            "cell": maestro_details.get("maestro_cell"),
            "testbench": maestro_details.get("maestro_testbench"),
            "corner_count": maestro_checks.get("corner_count"),
            "failed_corner_count": maestro_checks.get("failed_corner_count"),
        },
        "verification_scope": verification_scope,
        "residual_risks": residual_risks,
    }
    destination = Path(output_dir) if output_dir is not None else root / "reports"
    store = ArtifactStore(destination)
    json_path = store.write_json(destination / "design_report.json", report)
    lines = [
        "# SMIC180 Analog Design Report", "", f"Status: {report['status']}",
        f"Current confirmed state: `{state.current}`", "", "## Final Result", "",
        f"- Candidate hash: `{optimizer.get('candidate_hash') or 'unavailable'}`",
        f"- Result cell: `{report['publication']['result_cell'] or 'unverified'}`",
        f"- Final testbench: `{report['publication']['final_testbench'] or 'unverified'}`",
        f"- PVT: {report['pvt']['point_count']} points, {report['pvt']['failure_count']} failures",
        f"- Maestro history: `{report['maestro']['history'] or 'unverified'}`", "",
        "## Final Parameters", "",
    ]
    parameters = optimizer.get("parameters", {})
    lines.extend(f"- `{name}`: {value}" for name, value in sorted(parameters.items())) if parameters else lines.append("- No final optimizer parameters were bound.")
    lines.extend(["", "## Verified Metrics", ""])
    metrics = optimizer.get("metrics", {})
    lines.extend(f"- `{name}`: {value}" for name, value in sorted(metrics.items()) if _is_number(value)) if metrics else lines.append("- No optimizer metrics were bound.")
    lines.extend(["", "## Operating Point", ""])
    devices = optimizer.get("operating_point", {}).get("devices", {})
    if devices:
        for device, values in sorted(devices.items()):
            summary = ", ".join(f"{name}={value}" for name, value in sorted(values.items()) if name in {"gm", "gds", "gm_over_id", "intrinsic_gain", "saturation_margin", "vds", "vdsat"})
            lines.append(f"- `{device}`: {summary}")
    else:
        lines.append("- No optimizer operating-point data were bound.")
    history = report["optimization_history"]
    lines.extend(["", "## Optimization History", "", f"- Evaluations: {history['evaluation_count']}", f"- Successful: {history['success_count']}", f"- Failed: {history['failure_count']}", f"- Best objective: {history['best_objective']}", "", "## PVT Metric Ranges", ""])
    ranges = report["pvt"]["metric_ranges"]
    if ranges:
        for name, item in sorted(ranges.items()):
            lines.append(f"- `{name}`: min={item['minimum']} at `{item['minimum_point']}`, max={item['maximum']} at `{item['maximum_point']}`")
    else:
        lines.append("- No numeric PVT metric ranges were available.")
    lines.extend(["", "## Verification Scope", ""])
    phase_scope = report["verification_scope"]["phase_margin"]
    slew_scope = report["verification_scope"]["closed_loop_slew_rate"]
    if phase_scope["status"] == "verified":
        lines.append(f"- Phase margin: verified by STB, {phase_scope['value_deg']} deg.")
    else:
        lines.append(f"- Phase margin: unverified; {phase_scope['reason']}.")
    if slew_scope["status"] == "verified":
        lines.append(f"- Closed-loop slew rate: verified, rise={slew_scope['rise_v_per_s']} V/s, fall={slew_scope['fall_v_per_s']} V/s.")
    else:
        lines.append(f"- Closed-loop slew rate: unverified; {slew_scope['reason']}.")
    lines.extend(["", "## Stage Status", ""])
    lines.extend(f"- `{name}`: {status}" for name, status in stages.items())
    lines.extend(["", "Unverified items are not claims of success.", ""])
    markdown_path = store.write_text(destination / "design_report.md", "\n".join(lines))
    return json_path, markdown_path

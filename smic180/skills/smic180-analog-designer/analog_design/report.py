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


def _optimizer_context(root: Path) -> tuple[dict[str, Any], Path | None]:
    reference = _read_json(root / "optimizer" / "run_reference.json", {})
    workflow_path = reference.get("workflow_state") if isinstance(reference, dict) else None
    if not isinstance(workflow_path, str):
        return {}, None
    optimizer_root = Path(workflow_path).parent
    workflow = _read_json(Path(workflow_path), {})
    raw_metrics = workflow.get("best", {}).get("metrics", {})
    metrics = {
        name: value for name, value in raw_metrics.items()
        if isinstance(value, (int, float)) and (
            name.startswith("ac.") or "power" in name or "supply_current" in name
        )
    }
    saturation = {
        name: value for name, value in raw_metrics.items()
        if isinstance(value, (int, float)) and name.endswith(".saturation_margin")
    }
    return {
        "candidate_hash": reference.get("candidate_hash"),
        "state": workflow.get("state"),
        "parameters": workflow.get("best", {}).get("parameters", {}),
        "metrics": metrics,
        "operating_point": {
            "minimum_saturation_margin": min(saturation.values()) if saturation else None,
            "saturation_margins": saturation,
        },
    }, optimizer_root


def write_report(run_dir: str | Path) -> tuple[Path, Path]:
    root = Path(run_dir)
    state = WorkflowState.load(root / "workflow_state.json")
    current_index = _STATES.index(state.current)
    stages = {name: ("confirmed" if index <= current_index else "unverified") for index, name in enumerate(_STATES)}
    optimizer, optimizer_root = _optimizer_context(root)
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
        "pvt": {
            "overall_passed": pvt_raw.get("overall_passed") if isinstance(pvt_raw, dict) else None,
            "point_count": len(pvt_raw.get("points", [])) if isinstance(pvt_raw, dict) else 0,
            "failure_count": len(pvt_raw.get("failures", [])) if isinstance(pvt_raw, dict) else 0,
            "worst_by_spec": pvt_raw.get("worst_by_spec", {}) if isinstance(pvt_raw, dict) else {},
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
        "verification_scope": {
            "phase_margin": {"status": "unverified", "reason": "ordinary AC analysis is not an STB loop-stability measurement"},
            "closed_loop_slew_rate": {"status": "unverified", "reason": "open-loop transient slope is not the standard closed-loop slew-rate test"},
        },
        "residual_risks": [
            "Phase margin remains unverified until a dedicated STB loop testbench passes.",
            "Standard closed-loop slew rate remains unverified until a dedicated large-signal follower testbench passes.",
        ],
    }
    store = ArtifactStore(root)
    json_path = store.write_json(root / "reports" / "design_report.json", report)
    lines = [
        "# SMIC180 Analog Design Report", "",
        f"Status: {report['status']}",
        f"Current confirmed state: `{state.current}`", "",
        "## Final Result", "",
        f"- Candidate hash: `{optimizer.get('candidate_hash') or 'unavailable'}`",
        f"- Result cell: `{report['publication']['result_cell'] or 'unverified'}`",
        f"- Final testbench: `{report['publication']['final_testbench'] or 'unverified'}`",
        f"- PVT: {report['pvt']['point_count']} points, {report['pvt']['failure_count']} failures",
        f"- Maestro history: `{report['maestro']['history'] or 'unverified'}`", "",
        "## Verified Metrics", "",
    ]
    metrics = optimizer.get("metrics", {})
    if metrics:
        lines.extend(f"- `{name}`: {value}" for name, value in sorted(metrics.items()) if isinstance(value, (int, float)))
    else:
        lines.append("- No optimizer metrics were bound.")
    lines.extend(["", "## Verification Scope", "", "- Phase margin: unverified; ordinary AC is not STB.", "- Closed-loop slew rate: unverified; the existing open-loop transient slope is not a standard slew test.", "", "## Stage Status", ""])
    lines.extend(f"- `{name}`: {status}" for name, status in stages.items())
    lines.extend(["", "Unverified items are not claims of success.", ""])
    markdown_path = store.write_text(root / "reports" / "design_report.md", "\n".join(lines))
    return json_path, markdown_path
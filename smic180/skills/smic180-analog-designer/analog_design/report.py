"""Truthful machine-readable and engineering-readable design reports."""

from __future__ import annotations

from pathlib import Path

from .artifacts import ArtifactStore
from .workflow import _STATES, WorkflowState


def write_report(run_dir: str | Path) -> tuple[Path, Path]:
    root = Path(run_dir)
    state = WorkflowState.load(root / "workflow_state.json")
    current_index = _STATES.index(state.current)
    stages = {name: ("confirmed" if index <= current_index else "unverified") for index, name in enumerate(_STATES)}
    report = {"version": 1, "current_state": state.current, "stages": stages, "status": "complete" if state.current == "final_validation_passed" else "incomplete"}
    store = ArtifactStore(root)
    json_path = store.write_json(root / "reports" / "design_report.json", report)
    lines = ["# SMIC180 Analog Design Report", "", f"Status: {report['status']}", f"Current confirmed state: `{state.current}`", "", "## Stage Status", ""]
    lines.extend(f"- `{name}`: {status}" for name, status in stages.items())
    lines.extend(["", "Unverified stages are not claims of success.", ""])
    markdown_path = store.write_text(root / "reports" / "design_report.md", "\n".join(lines))
    return json_path, markdown_path

"""Additive audit snapshots for historical analog-design runs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Any

from .artifacts import ArtifactStore, file_sha256
from .report import write_report
from .schemas import circuit_ir_schema, design_spec_schema
from .workflow import DesignWorkflow


class AuditError(ValueError):
    """Raised when an additive audit snapshot cannot be created safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _calculation_report(run_dir: Path) -> str:
    try:
        sizing = json.loads((run_dir / "sizing" / "initial_sizing.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read initial sizing: {exc}") from exc
    records = sizing.get("records", {}) if isinstance(sizing, dict) else {}
    if not isinstance(records, dict):
        raise AuditError("initial sizing records are invalid")
    lines = ["# Initial Sizing Calculation Report", "", "Historical additive audit snapshot.", "", "Theoretical estimates are not declarations of legal PDK CDF values.", ""]
    for name, item in records.items():
        if not isinstance(item, dict):
            continue
        inputs = item.get("inputs", {}) if isinstance(item.get("inputs"), dict) else {}
        assumptions = item.get("assumptions", []) if isinstance(item.get("assumptions"), list) else []
        lines.extend([
            f"## {name}", "", f"- Formula: `{item.get('formula_id', 'unavailable')}`",
            f"- Dimension: `{item.get('dimension', 'unavailable')}`", f"- Value: `{item.get('value', 'unavailable')}`",
            f"- Status: `{item.get('status', 'unverified')}`", f"- Confidence: `{item.get('confidence', 'unverified')}`",
            "- Inputs: " + ", ".join(f"`{key}={value}`" for key, value in inputs.items()),
            "- Assumptions: " + "; ".join(str(value) for value in assumptions), "",
        ])
    return "\n".join(lines)


def _signed_control_paths(run_dir: Path) -> list[Path]:
    paths = [run_dir / "manifest.json", run_dir / "workflow_state.json"]
    paths.extend(sorted(run_dir.rglob("*.confirmed.json")))
    return sorted({path.resolve() for path in paths if path.is_file() and "audit" not in path.parts})


def _hashes(paths: list[Path], run_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in paths:
        try:
            name = str(path.relative_to(run_dir.resolve()))
        except ValueError:
            name = str(path)
        result[name] = file_sha256(path)
    return result


def write_audit_addendum(run_dir: str | Path) -> Path:
    root = Path(run_dir).resolve()
    workflow = DesignWorkflow.resume(root)
    final = root / "audit" / "addendum-v1"
    if final.exists():
        raise AuditError(f"audit addendum already exists: {final}")
    controls = _signed_control_paths(root)
    before = _hashes(controls, root)
    audit_root = root / "audit"
    audit_root.mkdir(parents=True, exist_ok=True)
    temporary = audit_root / f".addendum-v1-{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        store = ArtifactStore(temporary)
        spec_schema = store.write_json(temporary / "inputs" / "design_spec.schema.json", design_spec_schema())
        ir_schema = store.write_json(temporary / "ir" / "circuit_ir.schema.json", circuit_ir_schema())
        calculation = store.write_text(temporary / "sizing" / "calculation_report.md", _calculation_report(root))
        report_json, report_md = write_report(root, output_dir=temporary / "reports")
        generated = [spec_schema, ir_schema, calculation, report_json, report_md]
        after = _hashes(controls, root)
        if before != after:
            raise AuditError("historical signed control artifacts changed during audit")
        manifest = {
            "version": 1,
            "mode": "additive",
            "created_at": _utc_now(),
            "source_run": str(root),
            "source_state": workflow.state.current,
            "confirmation_chain_verified": True,
            "source_hashes_before": before,
            "source_hashes_after": after,
            "generated_artifacts": [
                {"path": str(path.relative_to(temporary)), "sha256": file_sha256(path), "size": path.stat().st_size}
                for path in generated
            ],
        }
        store.write_json(temporary / "migration_manifest.json", manifest)
        os.replace(temporary, final)
    except Exception:
        resolved = temporary.resolve()
        if resolved.parent == audit_root.resolve() and resolved.exists():
            shutil.rmtree(resolved)
        raise
    return final

import json
from pathlib import Path

import pytest

from analog_design.artifacts import ArtifactError, ArtifactStore


def test_create_run_builds_requested_layout_and_updates_latest_atomically(tmp_path):
    store = ArtifactStore(tmp_path)
    run = store.create_run("20260713_120000")
    assert run == tmp_path / "analog_design" / "20260713_120000"
    for relative in ("inputs", "topology", "sizing", "ir", "windows_sim/generated", "windows_sim/iterations", "frozen", "virtuoso", "equivalence", "simulator", "optimizer", "reports", "manifests", "audit"):
        assert (run / relative).is_dir()
    assert (tmp_path / "analog_design" / ".latest_run").read_text(encoding="utf-8").strip() == str(run.resolve())


def test_create_run_refuses_existing_directory(tmp_path):
    store = ArtifactStore(tmp_path)
    store.create_run("same")
    with pytest.raises(ArtifactError, match="already exists"):
        store.create_run("same")


def test_write_json_rejects_nonfinite_values_and_leaves_no_partial_file(tmp_path):
    store = ArtifactStore(tmp_path)
    target = tmp_path / "bad.json"
    with pytest.raises(ArtifactError, match="finite"):
        store.write_json(target, {"value": float("nan")})
    assert not target.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_confirmation_records_hashes_and_detects_tampering(tmp_path):
    store = ArtifactStore(tmp_path)
    artifact = tmp_path / "artifact.json"
    store.write_json(artifact, {"value": 1})
    marker = store.confirm(tmp_path / "confirmed.json", "unit_gate", [artifact])
    store.verify_confirmation(marker)
    artifact.write_text(json.dumps({"value": 2}), encoding="utf-8")
    with pytest.raises(ArtifactError, match="hash mismatch"):
        store.verify_confirmation(marker)


def test_write_json_accepts_read_only_mapping_values(tmp_path):
    from types import MappingProxyType
    store = ArtifactStore(tmp_path)
    target = store.write_json(tmp_path / "mapping.json", MappingProxyType({"nested": MappingProxyType({"value": 1})}))
    assert json.loads(target.read_text(encoding="utf-8")) == {"nested": {"value": 1}}


def test_stage_manifest_records_time_status_and_artifact_summaries(tmp_path):
    from analog_design.workflow import DesignWorkflow
    from test_ir_builder import load_spec

    load_spec(tmp_path)
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", tmp_path / "run")
    initialized = json.loads((workflow.run_dir / "manifests" / "000-initialized.json").read_text(encoding="utf-8"))
    assert initialized["status"] == "confirmed"
    assert initialized["timestamp"].endswith("Z")
    assert initialized["outputs"][0]["path"].replace("\\", "/").endswith("inputs/design_spec.json")

    workflow.validate_spec()
    transition = json.loads((workflow.run_dir / "workflow_state.json").read_text(encoding="utf-8"))["transitions"][0]
    assert transition["status"] == "confirmed"
    assert transition["timestamp"].endswith("Z")
    stage = json.loads((workflow.run_dir / transition["manifest"]).read_text(encoding="utf-8"))
    assert stage["stage"] == "spec_validated"
    assert stage["inputs"]
    assert {item["path"].replace("\\", "/").split("/")[-1] for item in stage["outputs"]} == {
        "design_spec.json", "design_spec.schema.json"
    }


def test_failure_manifest_records_failed_status_without_advancing(tmp_path):
    from analog_design.workflow import DesignWorkflow, WorkflowError
    from test_ir_builder import load_spec

    load_spec(tmp_path)
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", tmp_path / "run")
    workflow.validate_spec()
    with pytest.raises(WorkflowError):
        workflow.build_ir()
    failures = json.loads((workflow.run_dir / "failed_attempts.json").read_text(encoding="utf-8"))
    assert failures[-1]["status"] == "failed"
    assert failures[-1]["timestamp"].endswith("Z")
    failure_manifests = list((workflow.run_dir / "manifests").glob("failed-*.json"))
    assert len(failure_manifests) == 1
    assert json.loads(failure_manifests[0].read_text(encoding="utf-8"))["status"] == "failed"

def test_workflow_initialize_updates_latest_for_standard_output_layout_and_root_manifest(tmp_path):
    from analog_design.workflow import DesignWorkflow
    from test_ir_builder import load_spec

    load_spec(tmp_path)
    run_dir = tmp_path / "analog_design" / "run-001"
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", run_dir)
    assert (tmp_path / "analog_design" / ".latest_run").read_text(encoding="utf-8").strip() == str(run_dir.resolve())
    workflow.validate_spec()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "confirmed"
    assert manifest["current_stage"] == "spec_validated"
    assert manifest["updated_at"].endswith("Z")
    assert manifest["output_summary"]

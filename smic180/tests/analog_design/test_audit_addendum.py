import json

import pytest

from analog_design.audit import AuditError, write_audit_addendum
from analog_design.artifacts import file_sha256
from analog_design.workflow import DesignWorkflow
from test_ir_builder import load_spec


def test_audit_addendum_backfills_new_artifacts_without_changing_signed_history(tmp_path):
    load_spec(tmp_path)
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", tmp_path / "run")
    workflow.validate_spec()
    workflow.select_topology()
    workflow.calculate_initial_sizing()
    workflow.build_ir()
    workflow.render_netlist()

    signed = [
        workflow.run_dir / "inputs" / "spec_validated.confirmed.json",
        workflow.run_dir / "sizing" / "initial_sizing_complete.confirmed.json",
        workflow.run_dir / "ir" / "ir_validated.confirmed.json",
        workflow.run_dir / "workflow_state.json",
    ]
    before = {str(path): file_sha256(path) for path in signed}
    addendum = write_audit_addendum(workflow.run_dir)
    after = {str(path): file_sha256(path) for path in signed}

    assert before == after
    assert (addendum / "inputs" / "design_spec.schema.json").is_file()
    assert (addendum / "ir" / "circuit_ir.schema.json").is_file()
    assert (addendum / "sizing" / "calculation_report.md").is_file()
    assert (addendum / "reports" / "design_report.json").is_file()
    manifest = json.loads((addendum / "migration_manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "additive"
    assert manifest["source_state"] == "ir_validated"
    assert manifest["confirmation_chain_verified"] is True
    assert manifest["source_hashes_before"] == manifest["source_hashes_after"]
    assert all(item["sha256"] for item in manifest["generated_artifacts"])


def test_audit_addendum_refuses_to_overwrite_existing_addendum(tmp_path):
    load_spec(tmp_path)
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", tmp_path / "run")
    workflow.validate_spec()
    workflow.select_topology()
    workflow.calculate_initial_sizing()
    workflow.build_ir()
    write_audit_addendum(workflow.run_dir)
    with pytest.raises(AuditError, match="already exists"):
        write_audit_addendum(workflow.run_dir)

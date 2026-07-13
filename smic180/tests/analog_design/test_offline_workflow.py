import json

import pytest

from analog_design.workflow import DesignWorkflow, WorkflowError, WorkflowState
from test_ir_builder import confirmed_profile, load_spec


def test_state_machine_rejects_skipped_transition(tmp_path):
    state = WorkflowState.create(tmp_path / "workflow_state.json")
    with pytest.raises(WorkflowError, match="expected spec_validated"):
        state.advance("topology_selected", {})


def test_offline_workflow_persists_each_confirmed_stage_and_resume_hashes(tmp_path):
    spec = load_spec(tmp_path)
    spec_path = tmp_path / "spec.json"
    run_dir = tmp_path / "run"
    workflow = DesignWorkflow.initialize(spec_path, run_dir)
    assert workflow.state.current == "initialized"
    workflow.validate_spec()
    workflow.select_topology()
    workflow.calculate_initial_sizing()
    workflow.build_ir()
    workflow.render_netlist(model_includes=(("/models/core.scs", "tt"),))
    assert workflow.state.current == "ir_validated"
    assert (run_dir / "ir" / "circuit_ir.json").is_file()
    assert (run_dir / "windows_sim" / "generated" / "design.scs").is_file()
    resumed = DesignWorkflow.resume(run_dir)
    assert resumed.state.current == "ir_validated"
    (run_dir / "ir" / "circuit_ir.json").write_text("{}", encoding="utf-8")
    with pytest.raises(WorkflowError, match="hash mismatch"):
        DesignWorkflow.resume(run_dir)


def test_failed_attempt_does_not_advance_confirmed_state(tmp_path):
    spec = load_spec(tmp_path)
    run_dir = tmp_path / "run"
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", run_dir)
    workflow.validate_spec()
    with pytest.raises(WorkflowError):
        workflow.build_ir()
    assert workflow.state.current == "spec_validated"
    failures = json.loads((run_dir / "failed_attempts.json").read_text(encoding="utf-8"))
    assert failures[-1]["stage"] == "ir_validated"

def test_workflow_builds_physical_ir_and_real_netlist_from_confirmed_profile(tmp_path):
    load_spec(tmp_path)
    run_dir = tmp_path / "run"
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", run_dir)
    workflow.validate_spec()
    workflow.select_topology()
    workflow.calculate_initial_sizing()
    profile = confirmed_profile()

    ir = workflow.build_ir(technology=profile)
    deck = workflow.render_netlist(
        model_includes=profile.model_includes("/models/e2r018_v1p8_spe.scs", "tt"),
        technology=profile,
    )

    assert ir.technology["profile_state"] == "confirmed"
    assert ir.instance("M_IN_P").physical_parameters["finger_width"] > 0
    text = deck.read_text(encoding="utf-8")
    assert 'include "/models/e2r018_v1p8_spe.scs" section=mim_tt' in text
    assert "M_IN_P (N1 VINP NTAIL VSS) n33e2r" in text

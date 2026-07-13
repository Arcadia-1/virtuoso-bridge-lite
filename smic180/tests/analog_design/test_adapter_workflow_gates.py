from pathlib import Path

import pytest

from analog_design.workflow import DesignWorkflow, WorkflowError, WorkflowState


def make_state(tmp_path, current):
    path = tmp_path / "workflow_state.json"
    state = WorkflowState.create(path)
    order = [
        "spec_validated", "topology_selected", "initial_sizing_complete", "ir_validated",
        "windows_nominal_passed", "candidate_frozen", "schematic_created",
        "cdf_roundtrip_passed", "schematic_checked", "equivalence_passed",
    ]
    for target in order:
        marker = tmp_path / f"{target}.json"
        marker.write_text("{}", encoding="utf-8")
        state.advance(target, {})
        if target == current:
            break
    return DesignWorkflow(tmp_path, state)


def test_simulator_handoff_advances_only_from_equivalence(tmp_path):
    workflow = make_state(tmp_path, "equivalence_passed")
    pins = tmp_path / "pins.json"; pins.write_text("[]", encoding="utf-8")
    config = tmp_path / "sim.json"; config.write_text("{}", encoding="utf-8")
    review = tmp_path / "review.json"; review.write_text('{"required": true}', encoding="utf-8")
    workflow.record_simulator_handoff(pins, config, review)
    assert workflow.state.current == "simulator_validated"


def test_optimizer_preparation_does_not_claim_optimization_complete(tmp_path):
    workflow = make_state(tmp_path, "equivalence_passed")
    workflow.state.advance("simulator_validated", {})
    config = tmp_path / "opt.json"; config.write_text("{}", encoding="utf-8")
    baseline = tmp_path / "baseline.json"; baseline.write_text("{}", encoding="utf-8")
    evidence = tmp_path / "cdf.json"; evidence.write_text("{}", encoding="utf-8")
    workflow.record_optimizer_preparation(config, baseline, evidence)
    assert workflow.state.current == "simulator_validated"
    assert (tmp_path / "optimizer" / "prepared.confirmed.json").is_file()


def test_optimizer_completion_requires_external_confirmed_run_artifact(tmp_path):
    workflow = make_state(tmp_path, "equivalence_passed")
    workflow.state.advance("simulator_validated", {})
    with pytest.raises(WorkflowError, match="confirmed"):
        workflow.record_optimizer_completion(tmp_path / "missing.json")
    confirmed = tmp_path / "optimizer-result.confirmed.json"
    confirmed.write_text('{"state": "best_replayed"}', encoding="utf-8")
    workflow.record_optimizer_completion(confirmed)
    assert workflow.state.current == "optimization_complete"

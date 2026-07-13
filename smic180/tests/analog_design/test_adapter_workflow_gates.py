import json
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


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_simulator_preparation_does_not_claim_validation(tmp_path):
    workflow = make_state(tmp_path, "equivalence_passed")
    pins = tmp_path / "pins.json"; pins.write_text("[]", encoding="utf-8")
    config = tmp_path / "sim.json"; config.write_text("{}", encoding="utf-8")
    review = tmp_path / "review.json"; review.write_text('{"required": true}', encoding="utf-8")
    workflow.record_simulator_preparation(pins, config, review)
    assert workflow.state.current == "equivalence_passed"
    assert (tmp_path / "simulator" / "prepared.confirmed.json").is_file()


def test_simulator_validation_requires_passed_external_evidence(tmp_path):
    workflow = make_state(tmp_path, "equivalence_passed")
    failed = write_json(tmp_path / "simulator-result.json", {"status": "failed", "checks": {"spectre_passed": False}})
    with pytest.raises(WorkflowError, match="simulator evidence"):
        workflow.record_simulator_validation(failed)
    passed = write_json(tmp_path / "simulator-result.json", {
        "status": "passed",
        "checks": {"spectre_passed": True, "fresh_results": True, "measurements_readable": True},
    })
    workflow.record_simulator_validation(passed)
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


def test_optimizer_completion_requires_passing_fresh_replay_and_candidate_hash(tmp_path):
    workflow = make_state(tmp_path, "equivalence_passed")
    workflow.state.advance("simulator_validated", {})
    state = write_json(tmp_path / "workflow.json", {"state": "published", "candidate_hash": "abc"})
    replay = write_json(tmp_path / "replay.json", {"publishable": False, "best": {"objective": 1.0}})
    with pytest.raises(WorkflowError, match="fresh replay"):
        workflow.record_optimizer_completion(state, replay)
    replay = write_json(tmp_path / "replay.json", {"publishable": True, "best": {"objective": 0.0}})
    workflow.record_optimizer_completion(state, replay)
    assert workflow.state.current == "optimization_complete"


def test_pvt_publication_and_final_validation_require_distinct_passing_evidence(tmp_path):
    workflow = make_state(tmp_path, "equivalence_passed")
    workflow.state.advance("simulator_validated", {})
    state = write_json(tmp_path / "workflow.json", {"state": "published", "candidate_hash": "abc"})
    replay = write_json(tmp_path / "replay.json", {"publishable": True, "best": {"objective": 0.0}})
    workflow.record_optimizer_completion(state, replay)

    pvt = write_json(tmp_path / "pvt.json", {"overall_passed": True, "points": [{}] * 45, "failures": []})
    workflow.record_pvt_completion(pvt, expected_points=45)
    assert workflow.state.current == "pvt_passed"

    publication = write_json(tmp_path / "publication.confirmed.json", {"candidate_hash": "abc"})
    workflow.record_publication(state, publication)
    assert workflow.state.current == "published"

    final = write_json(tmp_path / "final.confirmed.json", {
        "status": "passed",
        "checks": {"spectre_passed": True, "fresh_results": True, "pvt_passed": True, "dut_uses_result": True},
        "details": {"candidate_hash": "abc"},
    })
    maestro = write_json(tmp_path / "maestro.confirmed.json", {
        "status": "passed",
        "checks": {"corner_count": 45, "failed_corner_count": 0, "maestro_run_completed": True, "reopen_check_passed": True},
        "details": {"history": "Interactive.3"},
    })
    workflow.record_final_validation(final, maestro, expected_points=45)
    assert workflow.state.current == "final_validation_passed"
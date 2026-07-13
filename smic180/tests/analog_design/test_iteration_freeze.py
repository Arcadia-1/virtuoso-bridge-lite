import json

import pytest

from analog_design.simulation.direct_spectre import DirectSpectreBackend
from analog_design.workflow import DesignWorkflow, WorkflowError
from test_direct_spectre_backend import FakeRunner, good_result
from test_ir_builder import load_spec


def prepared_workflow(tmp_path):
    load_spec(tmp_path)
    run_dir = tmp_path / "run"
    workflow = DesignWorkflow.initialize(tmp_path / "spec.json", run_dir)
    workflow.validate_spec()
    workflow.select_topology()
    workflow.calculate_initial_sizing()
    workflow.build_ir()
    workflow.render_netlist()
    return workflow


def backend(result):
    return DirectSpectreBackend(FakeRunner(result), ("gain", "ugbw", "slew_rate"), ("op", "ac", "tran"))


def test_simulate_advances_only_after_fresh_validated_result(tmp_path):
    workflow = prepared_workflow(tmp_path)
    workflow.simulate(backend(good_result()), iteration=1)
    assert workflow.state.current == "windows_nominal_passed"
    assert (workflow.run_dir / "windows_sim" / "measurements.json").is_file()


def test_failed_simulation_does_not_advance_state(tmp_path):
    workflow = prepared_workflow(tmp_path)
    result = good_result()
    result["measurements"]["gain"] = float("nan")
    with pytest.raises(WorkflowError, match="finite"):
        workflow.simulate(backend(result), iteration=1)
    assert workflow.state.current == "ir_validated"


def test_freeze_requires_hard_specs_or_explicit_near_feasible_permission(tmp_path):
    workflow = prepared_workflow(tmp_path)
    result = good_result()
    result["measurements"]["gain"] = 55.0
    workflow.simulate(backend(result), iteration=1)
    with pytest.raises(WorkflowError, match="hard specification"):
        workflow.freeze()
    workflow.freeze(allow_near_feasible=True, reason="optimizer baseline within finite search range")
    assert workflow.state.current == "candidate_frozen"
    manifest = json.loads((workflow.run_dir / "frozen" / "candidate_manifest.json").read_text(encoding="utf-8"))
    assert manifest["near_feasible"] is True
    assert manifest["reason"]


def test_freeze_copies_immutable_ir_and_deck_and_records_hashes(tmp_path):
    workflow = prepared_workflow(tmp_path)
    workflow.simulate(backend(good_result()), iteration=1)
    workflow.freeze()
    assert (workflow.run_dir / "frozen" / "circuit_ir.json").is_file()
    assert (workflow.run_dir / "frozen" / "design.scs").is_file()
    assert (workflow.run_dir / "frozen" / "candidate_frozen.confirmed.json").is_file()
    DesignWorkflow.resume(workflow.run_dir)

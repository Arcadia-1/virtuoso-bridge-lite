import json

from analog_design.netlist.equivalence import compare_metrics, compare_netlists
from analog_design.workflow import DesignWorkflow
from test_iteration_freeze import backend, prepared_workflow
from test_direct_spectre_backend import good_result
from test_netlist_equivalence import DIRECT, EXPORTED


def frozen_workflow(tmp_path):
    workflow = prepared_workflow(tmp_path)
    workflow.simulate(backend(good_result()), iteration=1)
    workflow.freeze()
    return workflow


def test_workflow_records_materialization_gates_and_equivalence(tmp_path):
    workflow = frozen_workflow(tmp_path)
    evidence = tmp_path / "run" / "virtuoso"
    evidence.mkdir(exist_ok=True)
    plan = evidence / "schematic_plan.json"
    cdf = evidence / "cdf_readback.json"
    check = evidence / "schcheck.json"
    exported = evidence / "exported_netlist.scs"
    plan.write_text("{}", encoding="utf-8")
    cdf.write_text("{}", encoding="utf-8")
    check.write_text('{"passed": true}', encoding="utf-8")
    exported.write_text(EXPORTED, encoding="utf-8")
    workflow.record_materialization(plan, cdf, check, exported)
    assert workflow.state.current == "schematic_checked"
    structural = compare_netlists(DIRECT, EXPORTED, parameter_defaults={"nch": {"m": 1.0}})
    metrics = compare_metrics({"gain": 60.0}, {"gain": 60.01}, {"gain": {"abs": 0.02, "rel": 0.0}})
    workflow.record_equivalence(structural, metrics)
    assert workflow.state.current == "equivalence_passed"
    DesignWorkflow.resume(workflow.run_dir)

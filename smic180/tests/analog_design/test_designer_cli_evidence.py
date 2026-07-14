import json

from analog_design.cli import main
from analog_design.workflow import DesignWorkflow, WorkflowState
from test_adapter_workflow_gates import make_state


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_cli_validate_simulator_consumes_real_passing_evidence(tmp_path):
    workflow = make_state(tmp_path / "run", "equivalence_passed")
    evidence = write_json(tmp_path / "simulator.json", {
        "status": "passed",
        "checks": {"spectre_passed": True, "fresh_results": True, "measurements_readable": True},
    })
    assert main(["validate-simulator", "--run-dir", str(workflow.run_dir), "--evidence", str(evidence)]) == 0
    assert DesignWorkflow.resume(workflow.run_dir).state.current == "simulator_validated"


def test_cli_bind_optimizer_run_advances_all_verified_external_gates(tmp_path):
    workflow = make_state(tmp_path / "run", "equivalence_passed")
    workflow.state.advance("simulator_validated", {})
    optimizer = tmp_path / "optimizer-run"
    write_json(optimizer / "workflow_state.json", {"state": "published", "candidate_hash": "abc"})
    write_json(optimizer / "result_manifest.json", {"publishable": True, "best": {"objective": 0.0}})
    write_json(optimizer / "pvt_results.json", {"overall_passed": True, "points": [{}] * 45, "failures": []})
    write_json(optimizer / "publication.confirmed.json", {"candidate_hash": "abc"})
    write_json(optimizer / "final_validation" / "final_validation.confirmed.json", {
        "status": "passed",
        "checks": {"spectre_passed": True, "fresh_results": True, "pvt_passed": True, "dut_uses_result": True},
        "details": {"candidate_hash": "abc"},
    })
    write_json(optimizer / "maestro_validation" / "maestro_validation.confirmed.json", {
        "status": "passed",
        "checks": {"corner_count": 45, "failed_corner_count": 0, "maestro_run_completed": True, "reopen_check_passed": True},
        "details": {"history": "Interactive.3"},
    })
    assert main([
        "bind-optimizer-run", "--run-dir", str(workflow.run_dir),
        "--optimizer-run-dir", str(optimizer), "--expected-pvt-points", "45",
    ]) == 0
    assert DesignWorkflow.resume(workflow.run_dir).state.current == "final_validation_passed"

def test_cli_bind_optimizer_run_consumes_profile_confirmations(tmp_path):
    workflow = make_state(tmp_path / "run", "equivalence_passed")
    workflow.state.advance("simulator_validated", {})
    optimizer = tmp_path / "optimizer-run"
    profile_hash = "p" * 64
    required = ["open_loop", "stability", "closed_loop_slew"]
    write_json(optimizer / "workflow_state.json", {"state": "published", "candidate_hash": "abc", "profile_summary_hash": profile_hash})
    write_json(optimizer / "result_manifest.json", {"publishable": True, "best": {"objective": 0.0}})
    write_json(optimizer / "pvt_results.json", {"overall_passed": True, "points": [{}] * 45, "failures": []})
    write_json(optimizer / "publication.confirmed.json", {"candidate_hash": "abc", "profile_summary_hash": profile_hash})
    final_checks = {profile_id: {name: True for name in ("result_exists", "final_tb_exists", "dut_uses_result", "netlist_uses_result", "spectre_passed", "pvt_passed", "fresh_results")} for profile_id in required}
    write_json(optimizer / "final_validation" / "final_validation.confirmed.json", {"version": 2, "status": "passed", "profiles": final_checks, "details": {"candidate_hash": "abc", "profile_summary_hash": profile_hash, "required_profile_ids": required}})
    maestro_profiles = {profile_id: {"test_exists": True, "run_completed": True, "history_exists": True, "reopen_check_passed": True, "metrics_match": True, "corner_count": 45, "failed_corner_count": 0} for profile_id in required}
    write_json(optimizer / "maestro_validation" / "maestro_validation.confirmed.json", {"version": 2, "status": "passed", "profiles": maestro_profiles, "details": {"profile_summary_hash": profile_hash, "required_profile_ids": required}})
    for profile_id in ("stability", "closed_loop_slew"):
        write_json(optimizer / (profile_id + ".confirmed.json"), {"profile_id": profile_id, "candidate_hash": "abc", "profile_summary_hash": profile_hash})
    assert main(["bind-optimizer-run", "--run-dir", str(workflow.run_dir), "--optimizer-run-dir", str(optimizer), "--expected-pvt-points", "45"]) == 0
    assert DesignWorkflow.resume(workflow.run_dir).state.current == "final_validation_passed"

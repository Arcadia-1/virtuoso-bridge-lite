import json

from analog_design.report import write_report
from analog_design.workflow import WorkflowState, _STATES


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_complete_report_summarizes_verified_results_and_keeps_unsupported_metrics_unverified(tmp_path):
    state = WorkflowState.create(tmp_path / "workflow_state.json")
    for target in _STATES[1:]:
        state.advance(target, {})
    write_json(tmp_path / "inputs" / "design_spec.json", {"circuit": {"type": "opamp"}, "metrics": []})
    write_json(tmp_path / "topology" / "topology_plan.json", {"id": "two_stage_miller"})
    write_json(tmp_path / "sizing" / "initial_sizing.json", {"confirmed_values": {}, "records": []})
    write_json(tmp_path / "windows_sim" / "measurements.json", {"gain": 60.0, "ugbw": 1e6})
    write_json(tmp_path / "equivalence" / "structural_comparison.json", {"equivalent": True})
    write_json(tmp_path / "equivalence" / "simulation_comparison.json", {"equivalent": True})
    optimizer = tmp_path / "external"
    write_json(optimizer / "workflow_state.json", {
        "state": "published", "candidate_hash": "abc",
        "best": {"parameters": {"input_pair_width": 6.3e-6}, "metrics": {
            "ac.ac_main.gain_dc_db": 64.9,
            "ac.ac_main.unity_gain_hz": 22e6,
            "op.M1.gm": 1.2e-3,
            "op.M1.gds": 2.0e-5,
            "op.M1.gm_over_id": 14.0,
            "op.M1.intrinsic_gain": 60.0,
            "op.M1.saturation_margin": 0.21,
            "op.M1.vds": 0.7,
            "op.M1.vdsat": 0.18,
        }},
    })
    write_json(optimizer / "search_history.json", {"history": [
        {"candidate_id": "candidate-000000", "objective": 0.4, "success": True, "failure": None},
        {"candidate_id": "candidate-000001", "objective": 0.0, "success": True, "failure": None},
    ]})
    write_json(optimizer / "result_manifest.json", {"failures": [], "publishable": True})
    pvt_points = [
        {"point_id": "tt-low", "corner": "tt", "voltage": 2.97, "temperature": -40,
         "metrics": {"ac.ac_main.gain_dc_db": 60.7, "ac.ac_main.unity_gain_hz": 10.1e6}},
        {"point_id": "ff-high", "corner": "ff", "voltage": 3.63, "temperature": 125,
         "metrics": {"ac.ac_main.gain_dc_db": 67.5, "ac.ac_main.unity_gain_hz": 41.9e6}},
    ] + [{}] * 43
    write_json(optimizer / "pvt_results.json", {"overall_passed": True, "points": pvt_points, "failures": []})
    write_json(optimizer / "final_validation" / "final_validation.confirmed.json", {
        "status": "passed", "details": {"result_cell": "result", "final_testbench": "result_tb", "candidate_hash": "abc"},
    })
    write_json(optimizer / "maestro_validation" / "maestro_validation.confirmed.json", {
        "status": "passed", "checks": {"corner_count": 45, "failed_corner_count": 0}, "details": {"history": "Interactive.3"},
    })
    write_json(tmp_path / "optimizer" / "run_reference.json", {
        "candidate_hash": "abc", "workflow_state": str(optimizer / "workflow_state.json"), "result_manifest": str(optimizer / "result_manifest.json"),
    })

    json_path, markdown_path = write_report(tmp_path)
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["optimizer"]["candidate_hash"] == "abc"
    assert report["pvt"]["point_count"] == 45
    assert report["publication"]["result_cell"] == "result"
    assert report["maestro"]["history"] == "Interactive.3"
    assert report["optimizer"]["parameters"]["input_pair_width"] == 6.3e-6
    assert report["optimizer"]["operating_point"]["devices"]["M1"]["gm_over_id"] == 14.0
    assert report["optimization_history"]["evaluation_count"] == 2
    assert report["pvt"]["metric_ranges"]["ac.ac_main.gain_dc_db"]["minimum"] == 60.7
    assert report["pvt"]["metric_ranges"]["ac.ac_main.unity_gain_hz"]["minimum_point"] == "tt-low"
    assert report["verification_scope"]["phase_margin"]["status"] == "unverified"
    assert report["verification_scope"]["closed_loop_slew_rate"]["status"] == "unverified"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Interactive.3" in markdown
    assert "Final Parameters" in markdown
    assert "Operating Point" in markdown
    assert "Optimization History" in markdown
    assert "PVT Metric Ranges" in markdown
    assert "Phase margin" in markdown
    assert "unverified" in markdown

def test_profile_report_promotes_metrics_only_when_all_hashes_match(tmp_path):
    state = WorkflowState.create(tmp_path / "workflow_state.json")
    for target in _STATES[1:]:
        state.advance(target, {})
    optimizer = tmp_path / "external"
    profile_hash = "p" * 64
    metrics = {
        "stb.stability.loop.phase_margin_deg": 62.0,
        "tran.closed_loop_slew.step.VOUT.slew_rise_v_per_s": 6.2e6,
        "tran.closed_loop_slew.step.VOUT.slew_fall_v_per_s": 5.9e6,
    }
    write_json(optimizer / "workflow_state.json", {"state": "published", "candidate_hash": "abc", "profile_summary_hash": profile_hash, "best": {"parameters": {}, "metrics": metrics}})
    write_json(optimizer / "result_manifest.json", {"failures": [], "publishable": True})
    write_json(optimizer / "pvt_results.json", {"overall_passed": True, "points": [], "failures": []})
    required = ["open_loop", "stability", "closed_loop_slew"]
    final_checks = {profile_id: {name: True for name in ("result_exists", "final_tb_exists", "dut_uses_result", "netlist_uses_result", "spectre_passed", "pvt_passed", "fresh_results")} for profile_id in required}
    write_json(optimizer / "final_validation" / "final_validation.confirmed.json", {"version": 2, "status": "passed", "profiles": final_checks, "details": {"candidate_hash": "abc", "profile_summary_hash": profile_hash, "required_profile_ids": required}})
    maestro_profiles = {profile_id: {"test_exists": True, "run_completed": True, "history_exists": True, "reopen_check_passed": True, "metrics_match": True, "corner_count": 45, "failed_corner_count": 0} for profile_id in required}
    write_json(optimizer / "maestro_validation" / "maestro_validation.confirmed.json", {"version": 2, "status": "passed", "profiles": maestro_profiles, "details": {"history": "Interactive.4", "profile_summary_hash": profile_hash, "required_profile_ids": required}})
    for profile_id in ("stability", "closed_loop_slew"):
        write_json(optimizer / (profile_id + ".confirmed.json"), {"version": 1, "profile_id": profile_id, "candidate_hash": "abc", "profile_summary_hash": profile_hash})
    write_json(tmp_path / "optimizer" / "run_reference.json", {"candidate_hash": "abc", "profile_summary_hash": profile_hash, "workflow_state": str(optimizer / "workflow_state.json"), "result_manifest": str(optimizer / "result_manifest.json")})
    report_path, _ = write_report(tmp_path, output_dir=tmp_path / "reports-valid")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["verification_scope"]["phase_margin"] == {"status": "verified", "value_deg": 62.0}
    assert report["verification_scope"]["closed_loop_slew_rate"] == {"status": "verified", "rise_v_per_s": 6.2e6, "fall_v_per_s": 5.9e6}
    write_json(optimizer / "closed_loop_slew.confirmed.json", {"version": 1, "profile_id": "closed_loop_slew", "candidate_hash": "abc", "profile_summary_hash": "x" * 64})
    report_path, _ = write_report(tmp_path, output_dir=tmp_path / "reports-stale")
    stale = json.loads(report_path.read_text(encoding="utf-8"))
    assert stale["verification_scope"]["phase_margin"]["status"] == "unverified"
    assert stale["verification_scope"]["closed_loop_slew_rate"]["status"] == "unverified"
    write_json(optimizer / "closed_loop_slew.confirmed.json", {"version": 1, "profile_id": "closed_loop_slew", "candidate_hash": "abc", "profile_summary_hash": profile_hash})
    workflow_value = json.loads((optimizer / "workflow_state.json").read_text(encoding="utf-8"))
    workflow_value["profile_summary_hash"] = "y" * 64
    write_json(optimizer / "workflow_state.json", workflow_value)
    report_path, _ = write_report(tmp_path, output_dir=tmp_path / "reports-workflow-stale")
    stale = json.loads(report_path.read_text(encoding="utf-8"))
    assert stale["verification_scope"]["phase_margin"]["status"] == "unverified"
    assert stale["verification_scope"]["closed_loop_slew_rate"]["status"] == "unverified"

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
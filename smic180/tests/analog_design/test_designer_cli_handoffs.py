import json

from analog_design.artifacts import ArtifactStore
from analog_design.cli import main
from analog_design.netlist.equivalence import compare_metrics, compare_netlists
from analog_design.workflow import DesignWorkflow
from test_handoff_workflow import frozen_workflow
from test_ir_builder import confirmed_profile
from analog_design.technology.base import write_technology_profile


DIRECT = """simulator lang=spectre
subckt amp A Z
M1 (Z A 0 0) nch w=10u l=1u
ends amp
"""
EXPORTED = """simulator lang=spectre
subckt amp A Z
M1 (Z A 0 0) nch l=1u w=10u
ends amp
"""


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def advance_to_equivalence(workflow):
    root = workflow.run_dir
    plan = write_json(root / "virtuoso" / "schematic_plan.json", {})
    cdf = write_json(root / "virtuoso" / "cdf_readback.json", {})
    check = write_json(root / "virtuoso" / "schcheck.json", {"passed": True})
    exported = root / "virtuoso" / "exported_netlist.scs"
    exported.write_text(DIRECT, encoding="utf-8")
    workflow.record_materialization(plan, cdf, check, exported)
    workflow.record_equivalence(
        {"equivalent": True, "differences": []},
        {"equivalent": True, "comparisons": {}},
    )


def test_cli_materialize_plan_only_writes_no_live_evidence(tmp_path, capsys):
    workflow = frozen_workflow(tmp_path)
    profile = tmp_path / "profile.json"
    write_technology_profile(profile, confirmed_profile())
    assert main([
        "materialize", "--run-dir", str(workflow.run_dir),
        "--technology-profile", str(profile), "--library", "lib",
        "--source-cell", "source", "--target-cell", "target", "--plan-only",
    ]) == 0
    assert '"status": "planned"' in capsys.readouterr().out
    assert workflow.state.current == "candidate_frozen"
    assert not (workflow.run_dir / "virtuoso" / "schematic_created.confirmed.json").exists()


def test_cli_verify_equivalence_compares_structure_and_fresh_metrics(tmp_path):
    workflow = frozen_workflow(tmp_path)
    root = workflow.run_dir
    plan = write_json(root / "virtuoso" / "schematic_plan.json", {})
    cdf = write_json(root / "virtuoso" / "cdf_readback.json", {})
    check = write_json(root / "virtuoso" / "schcheck.json", {"passed": True})
    direct = root / "frozen" / "design.scs"
    direct.write_text(DIRECT, encoding="utf-8")
    ArtifactStore(root).confirm(
        root / "frozen" / "candidate_frozen.confirmed.json", "candidate_frozen",
        [root / "frozen" / "circuit_ir.json", direct, root / "frozen" / "candidate_manifest.json"],
    )
    exported = root / "virtuoso" / "exported_netlist.scs"
    exported.write_text(EXPORTED, encoding="utf-8")
    workflow.record_materialization(plan, cdf, check, exported)
    left = write_json(tmp_path / "left.json", {"gain": 60.0})
    right = write_json(tmp_path / "right.json", {"gain": 60.01})
    tolerances = write_json(tmp_path / "tol.json", {"gain": {"abs": 0.02, "rel": 0.0}})
    assert main([
        "verify-equivalence", "--run-dir", str(root),
        "--direct-metrics", str(left), "--exported-metrics", str(right),
        "--tolerances", str(tolerances),
    ]) == 0
    assert DesignWorkflow.resume(root).state.current == "equivalence_passed"


def test_cli_prepare_simulator_records_preparation_without_validation(tmp_path):
    workflow = frozen_workflow(tmp_path)
    advance_to_equivalence(workflow)
    assert main([
        "prepare-simulator", "--run-dir", str(workflow.run_dir),
        "--library", "lib", "--cell", "source",
    ]) == 0
    resumed = DesignWorkflow.resume(workflow.run_dir)
    assert resumed.state.current == "equivalence_passed"
    assert (workflow.run_dir / "simulator" / "prepared.confirmed.json").is_file()


def test_cli_prepare_optimizer_records_schema_valid_handoff(tmp_path):
    workflow = frozen_workflow(tmp_path)
    advance_to_equivalence(workflow)
    workflow.state.advance("simulator_validated", {})
    ir = json.loads((workflow.run_dir / "frozen" / "circuit_ir.json").read_text(encoding="utf-8"))
    parameter = ir["parameters"][0]
    bias_mapping = write_json(tmp_path / "bias.json", {"tail_bias_voltage": "IBIAS"})
    evidence = write_json(tmp_path / "cdf.json", {
        parameter["id"]: {
            "instance": parameter["linked_instances"][0], "property": "w", "unit": "um",
            "linked_instances": parameter["linked_instances"][1:], "lower": 2e-6, "upper": 40e-6,
        }
    })
    profile_evidence = write_json(tmp_path / "profiles.json", {"profiles": [
        {"id": "open_loop", "role": "open_loop_small_signal", "testbench_cell": "open_tb", "dut_instance": "DUT", "stimuli": {"VDD": {"kind": "voltage", "value": 3.3, "source_instance": "SRC_VDD"}}, "analyses": [{"name": "ac_main", "type": "ac"}], "metrics": [], "specs": []},
        {"id": "stability", "role": "unity_gain_stability", "testbench_cell": "stb_tb", "dut_instance": "DUT", "stimuli": {"VDD": {"kind": "voltage", "value": 3.3, "source_instance": "SRC_VDD"}}, "analyses": [{"name": "loop", "type": "stb", "probe": "IPRB"}], "metrics": [], "specs": []},
        {"id": "closed_loop_slew", "role": "closed_loop_slew", "testbench_cell": "slew_tb", "dut_instance": "DUT", "stimuli": {"VDD": {"kind": "voltage", "value": 3.3, "source_instance": "SRC_VDD"}}, "analyses": [{"name": "step", "type": "tran"}], "metrics": [], "specs": []},
    ]})
    assert main([
        "prepare-optimizer", "--run-dir", str(workflow.run_dir), "--library", "lib",
        "--source-cell", "source", "--work-cell", "work", "--result-cell", "result",
        "--testbench-cell", "source_tb", "--cdf-evidence", str(evidence),
        "--bias-mapping", str(bias_mapping),
        "--profile-evidence", str(profile_evidence),
    ]) == 0
    assert workflow.state.current == "simulator_validated"
    raw = json.loads((workflow.run_dir / "optimizer" / "analog_opt_v2.json").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert [profile["id"] for profile in raw["verification_profiles"]] == ["open_loop", "stability", "closed_loop_slew"]
    assert (workflow.run_dir / "optimizer" / "prepared.confirmed.json").is_file()
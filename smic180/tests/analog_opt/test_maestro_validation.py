import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OPT = ROOT / "skills" / "smic180-simulator"
ENV = dict(os.environ, PYTHONPATH=str(OPT))


def run_code(code, *args):
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], env=ENV,
                          capture_output=True, text=True)


def published_run(tmp_path):
    design = {"library": "amp_text", "cell": "AMP", "work_cell": "AMP_work",
              "result_cell": "AMP_result", "testbench_cell": "AMP_tb", "dut_instance": "DUT"}
    (tmp_path / "workflow_state.json").write_text(json.dumps({"state": "published"}))
    (tmp_path / "publication.json").write_text(json.dumps({"candidate_hash": "abc", "parameters": {}}))
    (tmp_path / "publication.confirmed.json").write_text(json.dumps({"candidate_hash": "abc"}))
    (tmp_path / "analog_opt_config.resolved.json").write_text(json.dumps({
        "version": 2, "design": design,
        "pvt": {"corners": ["TT", "SS", "FF", "FNSP", "SNFP"],
                "voltages": [3.0, 3.3, 3.6], "temperatures_c": [-40, 27, 125],
                "voltage_stimulus": "VDD"},
        "stimuli": {"VDD": {"kind": "voltage", "value": 3.3,
                              "source_instance": "SRC_AVD"}},
    }))
    final = tmp_path / "final_validation"
    final.mkdir()
    (final / "final_validation.confirmed.json").write_text(json.dumps({"status": "passed"}))
    return tmp_path


def published_profile_run(tmp_path):
    published_run(tmp_path)
    config_path = tmp_path / "analog_opt_config.resolved.json"
    config = json.loads(config_path.read_text())
    profiles = []
    for profile_id, role, testbench, analysis_type in (
        ("open_loop", "open_loop", "AMP_open_loop_tb", "ac"),
        ("stability", "unity_gain_stability", "AMP_stability_tb", "stb"),
        ("closed_loop_slew", "closed_loop_slew", "AMP_slew_tb", "tran"),
    ):
        profiles.append({"id": profile_id, "role": role, "testbench_cell": testbench, "dut_instance": "DUT", "stimuli": {}, "analyses": [{"name": profile_id, "type": analysis_type}], "metrics": [], "specs": [], "pvt_policy": "full", "timeout_s": 1800})
    config["verification_profiles"] = profiles
    config_path.write_text(json.dumps(config))
    profile_hash = "p" * 64
    (tmp_path / "publication.json").write_text(json.dumps({"candidate_hash": "abc", "profile_summary_hash": profile_hash, "parameters": {}}))
    (tmp_path / "publication.confirmed.json").write_text(json.dumps({"candidate_hash": "abc", "profile_summary_hash": profile_hash}))
    details = {profile["id"]: {"final_testbench": "AMP_result_" + profile["id"] + "_tb", "pvt_point_count": 45} for profile in profiles}
    (tmp_path / "final_validation" / "final_validation.confirmed.json").write_text(json.dumps({"version": 2, "status": "passed", "profiles": {profile["id"]: {} for profile in profiles}, "details": {"required_profile_ids": [profile["id"] for profile in profiles], "profiles": details, "profile_summary_hash": profile_hash}}))
    return tmp_path


def test_maestro_context_requires_batch_confirmation(tmp_path):
    run = published_run(tmp_path)
    (run / "final_validation" / "final_validation.confirmed.json").unlink()
    result = run_code("from analog_opt.maestro_validation import load_maestro_context as f; f(__import__('sys').argv[1])", run)
    assert result.returncode != 0
    assert "batch Spectre" in result.stderr


def test_maestro_context_derives_isolated_names_and_45_corners(tmp_path):
    run = published_run(tmp_path)
    code = "from analog_opt.maestro_validation import load_maestro_context as f; c=f(__import__('sys').argv[1]); print(c.final_testbench,c.maestro_testbench,c.maestro_cell,len(c.corners),c.test_name)"
    result = run_code(code, run)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "AMP_result_tb AMP_result_maestro_tb AMP_result_maestro 45 amp_op_ac"


def test_maestro_context_builds_independent_tests_for_all_profiles(tmp_path):
    run = published_profile_run(tmp_path)
    code = "from analog_opt.maestro_validation import load_maestro_context as f; c=f(__import__('sys').argv[1]); print([(p.profile_id,p.test_name,p.final_testbench,p.maestro_testbench,p.analysis_types,p.expected_corner_count) for p in c.profiles])"
    result = run_code(code, run)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[('open_loop', 'open_loop', 'AMP_result_open_loop_tb', 'AMP_result_open_loop_maestro_tb', ('ac',), 45), ('stability', 'stability', 'AMP_result_stability_tb', 'AMP_result_stability_maestro_tb', ('stb',), 45), ('closed_loop_slew', 'closed_loop_slew', 'AMP_result_closed_loop_slew_tb', 'AMP_result_closed_loop_slew_maestro_tb', ('tran',), 45)]"


def test_maestro_corner_names_are_unique_and_explicit(tmp_path):
    run = published_run(tmp_path)
    code = "from analog_opt.maestro_validation import load_maestro_context as f; c=f(__import__('sys').argv[1]); print(len(set(x.name for x in c.corners)),c.corners[0].name,c.corners[-1].name)"
    result = run_code(code, run)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "45 TT_V3P0_TN40 SNFP_V3P6_TP125"


def test_maestro_confirmation_requires_history_reopen_and_45_points(tmp_path):
    run = published_run(tmp_path)
    code = "from analog_opt.maestro_validation import write_maestro_confirmation as f; f(__import__('sys').argv[1], {'maestro_cell_exists':True,'maestro_testbench_exists':True,'test_exists':True,'dut_uses_result_cell':True,'model_sections_verified':True,'corner_count':45,'maestro_run_completed':True,'failed_corner_count':0,'history_exists':True,'reopen_check_passed':True}, {})"
    result = run_code(code, run)
    assert result.returncode == 0, result.stderr
    payload = json.loads((run / "maestro_validation" / "maestro_validation.confirmed.json").read_text())
    assert payload["status"] == "passed"


def test_maestro_profile_confirmation_requires_three_histories(tmp_path):
    run = published_run(tmp_path)
    checks = {
        "open_loop": {"test_exists": True, "run_completed": True, "history_exists": True, "reopen_check_passed": True, "metrics_match": True, "corner_count": 45, "failed_corner_count": 0},
        "stability": {"test_exists": True, "run_completed": True, "history_exists": True, "reopen_check_passed": True, "metrics_match": True, "corner_count": 45, "failed_corner_count": 0},
    }
    code = "from analog_opt.maestro_validation import write_maestro_profile_confirmation as f; import json,sys; f(sys.argv[1],json.loads(sys.argv[2]),{'required_profile_ids':['open_loop','stability','closed_loop_slew'],'global_checks':{k:True for k in ('maestro_cell_exists','maestro_testbenches_exist','dut_uses_result_cell','model_sections_verified','profile_summary_hash_match')}})"
    result = run_code(code, run, json.dumps(checks))
    assert result.returncode != 0 and "closed_loop_slew" in result.stderr


def test_maestro_profile_confirmation_requires_global_structural_gates(tmp_path):
    run = published_run(tmp_path)
    profile_checks = {name: {"test_exists": True, "run_completed": True, "history_exists": True, "reopen_check_passed": True, "metrics_match": True, "corner_count": 45, "failed_corner_count": 0} for name in ("open_loop", "stability", "closed_loop_slew")}
    details = {"required_profile_ids": list(profile_checks), "global_checks": {"maestro_cell_exists": True}}
    code = "from analog_opt.maestro_validation import write_maestro_profile_confirmation as f; import json,sys; f(sys.argv[1],json.loads(sys.argv[2]),json.loads(sys.argv[3]))"
    result = run_code(code, run, json.dumps(profile_checks), json.dumps(details))
    assert result.returncode != 0 and "global" in result.stderr


def test_maestro_profile_metrics_match_direct_spectre_with_explicit_tolerances():
    direct = {
        "stability": {"phase_margin_deg": 62.0, "gain_margin_db": 15.0},
        "closed_loop_slew": {"slew_rise_v_per_s": 2.0e6, "slew_fall_v_per_s": -1.8e6},
    }
    maestro = {
        "stability": {"phase_margin_deg": 62.01, "gain_margin_db": 15.001},
        "closed_loop_slew": {"slew_rise_v_per_s": 2.0005e6, "slew_fall_v_per_s": -1.8004e6},
    }
    code = "from analog_opt.maestro_validation import compare_profile_metrics as f; import json,sys; print(json.dumps(f(json.loads(sys.argv[1]),json.loads(sys.argv[2]),relative=1e-3,absolute=0.02),sort_keys=True))"
    result = run_code(code, json.dumps(direct), json.dumps(maestro))
    assert result.returncode == 0, result.stderr
    comparison = json.loads(result.stdout)
    assert comparison["passed"] is True and comparison["profiles"]["stability"]["passed"] is True
    maestro["stability"]["phase_margin_deg"] = 60.0
    result = run_code(code, json.dumps(direct), json.dumps(maestro))
    assert json.loads(result.stdout)["passed"] is False


def test_cli_exposes_create_and_verify_maestro_commands():
    script = OPT / "scripts" / "analog_optimize.py"
    result = subprocess.run([sys.executable, str(script), "--help"], env=ENV,
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "create-maestro" in result.stdout
    assert "verify-maestro" in result.stdout


def test_maestro_run_mode_is_capability_guarded_for_ic618():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.create_maestro); assert \"getd('maeSetCurrentRunMode)\" in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr

def test_maestro_testbench_materializes_resolved_stimuli():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m._copy_maestro_testbench); assert '_maestro_stimulus_plan' in s and 'published.parameters' in s and 'SRC_AVD' not in s and '10u' not in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr


def test_ic618_run_mode_uses_axl_fallback():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.create_maestro); assert 'axlSetCurrentRunMode' in s and 'axlGetMainSetupDB' in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr

def test_ic618_history_log_parser_extracts_named_corners():
    code = "from analog_opt.maestro_validation_live import parse_history_log as f; text='amp_op_ac\\tcorner\\tTT_V3P3_TP27 - \\nGAIN_DC_DB\\t\\t67.0\\tYes\\nBW_3DB_HZ\\t\\t28K\\nUNITY_GAIN_HZ\\t\\t64M\\tYes\\nNumber of simulation errors: 0\\nInteractive.2 completed.'; r=f(text,('TT_V3P3_TP27',)); print(len(r['points']),r['simulation_errors'],r['completed'])"
    result = run_code(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1 0 True"


def test_reopen_check_uses_ic618_axl_queries():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.verify_maestro); assert 'axlGetTests' in s and 'axlGetCorners' in s and 'maeGetTests' not in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr

def test_maestro_live_uses_bridge_lite_public_api_only():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m); required=('open_gui_session','create_test','setup_corner','run_and_wait','read_results','save_setup'); assert all(x in s for x in required); assert 'virtuoso-bridge-lite' not in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr


def test_cli_exposes_repair_maestro_models_command():
    script = OPT / "scripts" / "analog_optimize.py"
    result = subprocess.run([sys.executable, str(script), "--help"], env=ENV,
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "repair-maestro-models" in result.stdout


def test_legacy_maestro_xml_repair_removes_only_redundant_core_alias():
    xml = """<setupdb><active><corners><corner>TT<models><model>models.scs<modelsection>tt</modelsection></model><model>models.scs__core<modelsection>tt</modelsection></model><model>models.scs__mim<modelsection>mim_tt</modelsection></model></models></corner></corners></active></setupdb>"""
    code = "from analog_opt.maestro_validation_live import _remove_redundant_core_models as f; import sys; print(f(sys.argv[1],'models.scs',('TT',)))"
    result = run_code(code, xml)
    assert result.returncode == 0, result.stderr
    repaired = result.stdout
    assert "models.scs__core" not in repaired
    assert ">models.scs<" in repaired
    assert "models.scs__mim" in repaired
    assert "mim_tt" in repaired

def test_model_repair_requires_manifest_and_writes_audit_artifact():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.repair_maestro_models); assert all(x in s for x in ('maestro_manifest.json','_configure_corner_models','maestro_model_repair.json','save_setup'))"
    result = run_code(code)
    assert result.returncode == 0, result.stderr

def test_cli_exposes_preflight_and_accept_history_commands():
    script = OPT / "scripts" / "analog_optimize.py"
    result = subprocess.run([sys.executable, str(script), "--help"], env=ENV,
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "preflight-maestro" in result.stdout
    assert "accept-maestro-history" in result.stdout


def test_netlist_stimulus_verifier_accepts_physical_values():
    code = "from analog_opt.maestro_validation import verify_maestro_netlist as f; print(f('DUT (A) AMP_result\\nSRC_AVD (A 0) vsource dc=VDD type=dc\\nPVSS_AVS (G 0) vsource dc=0 type=dc\\nSRC_VIN (I 0) vsource dc=0.75 type=dc\\nSRC_VIP (P 0) vsource dc=0.75 mag=1 phase=0 type=dc\\nSRC_IBIAS (B 0) isource dc=10u type=dc\\nLOAD_VOUT (O 0) capacitor c=1p','AMP_result','VDD')['SRC_IBIAS'])"
    result = run_code(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1e-05"


def test_netlist_stimulus_verifier_rejects_stale_bias():
    code = "from analog_opt.maestro_validation import verify_maestro_netlist as f; f('DUT (A) AMP_result\\nSRC_AVD (A 0) vsource dc=VDD type=dc\\nPVSS_AVS (G 0) vsource dc=0 type=dc\\nSRC_VIN (I 0) vsource dc=0.75 type=dc\\nSRC_VIP (P 0) vsource dc=0.75 mag=1 phase=0 type=dc\\nSRC_IBIAS (B 0) isource type=sine\\nLOAD_VOUT (O 0) capacitor c=1p','AMP_result','VDD')"
    result = run_code(code)
    assert result.returncode != 0
    assert "SRC_IBIAS" in result.stderr


def test_corner_status_contains_failure_categories():
    code = "from analog_opt.maestro_validation import build_corner_status as f; p={'corner':'TT_V3P3_TP27','outputs':{'GAIN_DC_DB':{'value':'67','pass_fail':'Yes'},'UNITY_GAIN_HZ':{'value':'64M','pass_fail':'Yes'}}}; print(f(p, spectre_completed=True, spectre_errors=0)['failure_category'])"
    result = run_code(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "none"


def test_preflight_module_records_capabilities_and_reextraction_state():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.preflight_maestro); assert all(x in s for x in ('maestro_capabilities.json','maestro_preflight.json','maestro_extraction.json','verify_maestro_netlist','ASSEMBLER-9039'))"
    result = run_code(code)
    assert result.returncode == 0, result.stderr


def test_preflight_classifies_an_uncreated_delivery_without_opening_gui():
    code = "from analog_opt.maestro_validation_live import _preflight_action as f; print(f(False,False,False)); print(f(True,True,True))"
    result = run_code(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["capabilities_only", "full"]


def test_maestro_stimulus_plan_uses_resolved_sources_and_published_bias():
    config = {
        "parameters": [{"name": "tail_bias_voltage", "target": "bias", "stimulus": "IBIAS"}],
        "stimuli": {
            "VDD": {"kind": "voltage", "value": 3.3, "source_instance": "SRC_VDD"},
            "VINP": {"kind": "voltage", "dc": 1.65, "ac": 1.0, "source_instance": "SRC_VINP"},
            "VINN": {"kind": "voltage", "dc": 1.65, "ac": 0.0, "source_instance": "SRC_VINN"},
            "IBIAS": {"kind": "voltage", "value": 0.9, "source_instance": "SRC_IBIAS"},
            "VSS": {"kind": "voltage", "value": 0.0, "source_instance": "PVSS_VSS"},
        },
    }
    code = "from analog_opt.maestro_validation_live import _maestro_stimulus_plan as f; import json,sys; print(json.dumps(f(json.loads(sys.argv[1]),{'tail_bias_voltage':1.2},'VDD')))"
    result = run_code(code, json.dumps(config))
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert ["SRC_VDD", "vdc", "VDD"] in plan
    assert ["SRC_VINP", "vdc", "1.65"] in plan
    assert ["SRC_VINP", "acm", "1"] in plan
    assert ["SRC_VINN", "vdc", "1.65"] in plan
    assert ["SRC_IBIAS", "vdc", "1.2"] in plan
    assert ["PVSS_VSS", "vdc", "0"] in plan
    assert all("SRC_AVD" not in item and "10u" not in item for item in plan)


def test_profile_maestro_copy_only_parameterizes_supply_and_preserves_pulse_source():
    profile = {"stimuli": {"VDD": {"kind": "voltage", "source_instance": "SRC_VDD"}, "VIN_STEP": {"kind": "voltage", "source_instance": "VIN_STEP", "type": "pulse"}}}
    code = "from analog_opt.maestro_validation_live import _maestro_profile_stimulus_plan as f; import json,sys; p=json.loads(sys.argv[1]); print(json.dumps(f(type('P',(),p)(),'VDD')))"
    result = run_code(code, json.dumps(profile))
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan == [["SRC_VDD", "vdc", "VDD"], ["SRC_VDD", "srcType", "dc"]]


def test_existing_maestro_testbench_requires_verified_recovery():
    code = "from analog_opt.maestro_validation_live import _copy_action as f; print(f(False,False)); print(f(True,True)); f(True,False)"
    result = run_code(code)
    assert result.returncode != 0
    assert result.stdout.strip().splitlines() == ["copy", "resume"]
    assert "not structurally equivalent" in result.stderr

def test_create_accepts_only_preflight_artifacts_in_existing_root(tmp_path):
    root = tmp_path / "maestro_validation"
    root.mkdir()
    (root / "maestro_capabilities.json").write_text("{}")
    (root / "maestro_preflight.json").write_text("{}")
    code = "from analog_opt.maestro_validation_live import _prepare_create_root as f; f(__import__('sys').argv[1]); print('ok')"
    result = run_code(code, root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    (root / "unexpected.json").write_text("{}")
    result = run_code(code, root)
    assert result.returncode != 0
    assert "existing Maestro artifacts" in result.stderr

def test_maestro_nominal_model_files_include_tt_and_mim_tt():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m._nominal_model_options); print(m._nominal_model_options('/p/models.scs')); assert '\"tt\"' in s and '\"mim_tt\"' in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr
    assert "mim_tt" in result.stdout

def test_smic180_maestro_corner_models_include_process_and_mim_sections():
    code = "from analog_opt.maestro_validation_live import _corner_model_sections as f; print(f('TT')); print(f('FF')); print(f('SS')); print(f('FNSP')); print(f('SNFP'))"
    result = run_code(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "('tt', 'mim_tt')",
        "('ff', 'mim_ff')",
        "('ss', 'mim_ss')",
        "('fnsp', 'mim_tt')",
        "('snfp', 'mim_tt')",
    ]


def test_maestro_corner_writer_uses_distinct_core_and_mim_model_aliases():
    code = "from analog_opt.maestro_validation_live import _corner_models_skill as f; s=f('S','C','/p/models.scs','FNSP'); print(s); assert 'models.scs__core' in s and 'models.scs__mim' in s and 'fnsp' in s and 'mim_tt' in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr

def test_preflight_reuses_the_resolved_stimulus_plan():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.preflight_maestro); assert '_maestro_stimulus_plan' in s and '_skill_list' in s and 'SRC_AVD' not in s and '10u' not in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr

def test_accept_history_writes_corner_status_file():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.accept_maestro_history); assert 'maestro_corner_status.json' in s and 'build_corner_status' in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr


def test_accept_history_cannot_bypass_multi_profile_detail_table_validation():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.accept_maestro_history); assert 'multi-profile Maestro validation requires verify-maestro' in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr


def test_saved_maestro_model_matrix_verifies_each_corner_section():
    xml = "<setupdb><active><corners><corner>FNSP<models><model>models.scs__core<modelsection>fnsp</modelsection></model><model>models.scs__mim<modelsection>mim_tt</modelsection></model></models></corner></corners></active></setupdb>"
    code = "from analog_opt.maestro_validation_live import _verify_model_matrix_xml as f; print(f(__import__('sys').argv[1],{'FNSP':('fnsp','mim_tt')}))"
    result = run_code(code, xml)
    assert result.returncode == 0, result.stderr
    result = run_code(code, xml.replace("mim_tt", "mim_ff"))
    assert result.returncode != 0 and "FNSP" in result.stderr


def test_legacy_preflight_cannot_claim_multi_profile_validation():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.preflight_maestro); assert 'multi-profile Maestro preflight requires verify-maestro' in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr


def test_create_maestro_materializes_each_profile_test_and_analysis():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.create_maestro); assert 'for profile in context.profiles' in s and '_copy_maestro_testbench(client, context, profile)' in s and '_configure_profile_analyses' in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr


def test_verify_maestro_uses_profile_metric_comparison_and_profile_confirmation():
    code = "import inspect,analog_opt.maestro_validation_live as m; s=inspect.getsource(m.verify_maestro); assert '_verify_profile_maestro' in s; h=inspect.getsource(m._verify_profile_maestro); assert 'compare_profile_metrics' in h and 'write_maestro_profile_confirmation' in h and 'context.profiles' in h"
    result = run_code(code)
    assert result.returncode == 0, result.stderr


def test_profile_comparison_inputs_align_direct_pvt_and_maestro_outputs(tmp_path):
    profile_root = tmp_path / "final_validation" / "profiles" / "stability"
    profile_root.mkdir(parents=True)
    (profile_root / "pvt_results.json").write_text(json.dumps({"points": [{"corner": "tt", "voltage": 3.3, "temperature": 27.0, "metrics": {"measured": {"phase_margin_deg": 62.0}}}]}))
    manifest = {"profiles": [{"profile_id": "stability", "metrics": [{"metric": "phase_margin_deg", "output": "stability__phase_margin_deg"}]}]}
    results = {"points": [{"outputs": {"stability__phase_margin_deg": {"value": "62.01", "pass_fail": "Pass"}}}]}
    code = "from analog_opt.maestro_validation_live import _profile_comparison_inputs as f; from types import SimpleNamespace as S; from pathlib import Path; import json,sys; root=Path(sys.argv[1]); p=S(profile_id='stability',expected_corner_count=1); c=S(process='TT',voltage=3.3,temperature=27.0,name='TT_V3P3_TP27'); ctx=S(run_dir=root,profiles=(p,),corners=(c,)); d,o,checks,counts=f(ctx,json.loads(sys.argv[2]),json.loads(sys.argv[3])); print(json.dumps({'direct':d,'observed':o,'checks':checks,'counts':counts},sort_keys=True))"
    result = run_code(code, tmp_path, json.dumps(manifest), json.dumps(results))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["direct"]["stability@TT_V3P3_TP27"]["phase_margin_deg"] == 62.0
    assert payload["observed"]["stability@TT_V3P3_TP27"]["phase_margin_deg"] == 62.01


def test_profile_analysis_plan_requires_explicit_stb_maestro_options():
    code = "from analog_opt.maestro_validation_live import _profile_analysis_plan as f; import json,sys; print(json.dumps(f([{'name':'loop','type':'stb','probe':'IPRB','start':1.0,'stop':1e9,'points_per_decade':50}])))"
    result = run_code(code)
    assert result.returncode != 0 and "maestro_options" in result.stderr
    code = "from analog_opt.maestro_validation_live import _profile_analysis_plan as f; import json; print(json.dumps(f([{'name':'loop','type':'stb','maestro_options':'((\"probe\" \"IPRB\"))'}])))"
    result = run_code(code)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[0]["type"] == "stb"


def test_profile_metric_plan_requires_expression_for_hard_spec_metric():
    profile = {"profile_id": "stability", "metrics": [{"name": "phase_margin_deg"}], "specs": [{"metric": "phase_margin_deg", "hard": True}]}
    code = "from analog_opt.maestro_validation_live import _profile_metric_plan as f; import json,sys; p=json.loads(sys.argv[1]); print(f(type('P',(),p)()))"
    result = run_code(code, json.dumps(profile))
    assert result.returncode != 0 and "maestro_expression" in result.stderr
    profile["metrics"][0]["maestro_expression"] = "phaseMargin(loopGain)"
    result = run_code(code, json.dumps(profile))
    assert result.returncode == 0, result.stderr
    assert "stability__phase_margin_deg" in result.stdout


def test_maestro_json_artifact_is_parseable_and_newline_terminated(tmp_path):
    target = tmp_path / "artifact.json"
    code = "from analog_opt.maestro_validation_live import _write_json; from pathlib import Path; import sys; _write_json(Path(sys.argv[1]), {'status':'passed'})"
    result = run_code(code, target)
    assert result.returncode == 0, result.stderr
    text = target.read_text(encoding="utf-8")
    assert text.endswith("\n") and not text.endswith("\\n")
    assert json.loads(text) == {"status": "passed"}

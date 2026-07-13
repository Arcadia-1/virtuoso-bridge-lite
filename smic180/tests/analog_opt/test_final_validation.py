import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "skills" / "smic180-simulator"


def run_code(code, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], text=True, capture_output=True, env=env)


def published_run(tmp_path):
    config = {"version": 2, "design": {"library": "tr", "cell": "amp", "work_cell": "amp_work",
              "result_cell": "amp_result", "testbench_cell": "amp_baseline_tb", "dut_instance": "DUT"},
              "stimuli": {}, "parameters": [], "analyses": [], "metrics": [], "specs": [],
              "search": {}, "pvt": {}, "outputs": {"run_dir": str(tmp_path)}}
    (tmp_path / "analog_opt_config.resolved.json").write_text(json.dumps(config))
    (tmp_path / "publication.json").write_text(json.dumps({"candidate_hash": "abc123", "parameters": {}}))
    (tmp_path / "publication.confirmed.json").write_text(json.dumps({"candidate_hash": "abc123"}))
    (tmp_path / "workflow_state.json").write_text(json.dumps({"state": "published"}))


def test_final_validation_module_is_isolated_to_optimizer_v2():
    result = run_code("import analog_opt.final_validation as m; print(m.__file__)")
    assert result.returncode == 0, result.stderr
    assert "smic180-simulator" in result.stdout


def test_final_validation_rejects_unpublished_run(tmp_path):
    (tmp_path / "workflow_state.json").write_text(json.dumps({"state": "validated"}))
    code = "from analog_opt.final_validation import load_published_context; load_published_context(__import__('sys').argv[1])"
    result = run_code(code, tmp_path)
    assert result.returncode != 0 and "published" in result.stderr


def test_final_validation_derives_isolated_final_tb(tmp_path):
    published_run(tmp_path)
    code = "from analog_opt.final_validation import load_published_context as f; c=f(__import__('sys').argv[1]); print(c.library,c.result_cell,c.baseline_testbench,c.final_testbench)"
    result = run_code(code, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "tr amp_result amp_baseline_tb amp_result_tb"


def test_final_netlist_must_reference_result_and_reject_work_cell():
    good = run_code("from analog_opt.final_validation import verify_netlist_text as f; f('DUT (A) amp_result','amp_result','amp_work')")
    missing = run_code("from analog_opt.final_validation import verify_netlist_text as f; f('DUT (A) amp','amp_result','amp_work')")
    stale = run_code("from analog_opt.final_validation import verify_netlist_text as f; f('DUT (A) amp_result X amp_work','amp_result','amp_work')")
    assert good.returncode == 0
    assert missing.returncode != 0 and "result cell" in missing.stderr
    assert stale.returncode != 0 and "work cell" in stale.stderr


def test_final_confirmation_requires_all_checks(tmp_path):
    code = "import json,sys; from analog_opt.final_validation import write_confirmation as f; c={k:True for k in ('result_exists','final_tb_exists','dut_uses_result','netlist_uses_result','spectre_passed','pvt_passed','fresh_results')}; print(f(sys.argv[1],c,{}))"
    result = run_code(code, tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads((tmp_path / "final_validation" / "final_validation.confirmed.json").read_text())
    assert payload["status"] == "passed"


def test_existing_optimizer_workflow_is_not_imported_by_final_validation():
    code = "import inspect,analog_opt.final_validation as m; s=inspect.getsource(m); assert 'OptimizationWorkflow' not in s and 'publish_result_cell' not in s"
    assert run_code(code).returncode == 0


def test_persistent_adapter_copies_prepared_tb_without_changing_base_adapter():
    code = "import inspect; from analog_opt.final_validation_live import PersistentFinalNetlistAdapter as C; s=inspect.getsource(C); assert 'super()._prepare_tb()' in s and 'FINAL_TB_COPY_OK' in s and '_final_testbench_copied' in s and 'def _delete_tb' not in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr


def test_persistent_adapter_copies_final_testbench_only_once():
    code = "import inspect; from analog_opt.final_validation_live import PersistentFinalNetlistAdapter as C; s=inspect.getsource(C._prepare_tb); assert 'if self._final_testbench_copied:' in s and 'self._final_testbench_copied = True' in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr

def test_final_pvt_uses_evaluation_result_contract():
    code = "import inspect, analog_opt.final_validation_live as m; s=inspect.getsource(m); assert 'EvaluationResult(point.point_id' in s and 'type(\"R\"' not in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr

def test_cli_exposes_verify_result_without_removing_existing_commands():
    script = ROOT / "scripts" / "analog_optimize.py"
    result = subprocess.run([sys.executable, str(script), "--help"], text=True, capture_output=True)
    assert result.returncode == 0
    for name in ("validate", "evaluate", "run", "resume", "report", "verify-result"):
        assert name in result.stdout

def test_cli_prefers_its_own_analog_opt_package_when_simulator_is_first():
    script = ROOT / "scripts" / "analog_optimize.py"
    simulator = ROOT.parent / "smic180-simulator"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(simulator) + os.pathsep + str(ROOT)
    result = subprocess.run([sys.executable, str(script), "verify-result", "--run-dir", "missing"], text=True, capture_output=True, env=env)
    assert "No module named 'analog_opt.final_validation_live'" not in result.stderr

def test_final_validation_replays_published_bias_parameters():
    code = "from analog_opt.final_validation_live import _published_biases as f; c={'parameters':[{'name':'tail_bias_voltage','target':'bias','stimulus':'IBIAS'},{'name':'W','target':'virtuoso_cdf'}]}; print(f(c,{'tail_bias_voltage':1.2,'W':6e-6}))"
    result = run_code(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{'IBIAS': 1.2}"


def test_published_context_preserves_publication_parameters(tmp_path):
    published_run(tmp_path)
    (tmp_path / "publication.json").write_text(json.dumps({"candidate_hash": "abc123", "parameters": {"tail_bias_voltage": 1.2}}))
    code = "from analog_opt.final_validation import load_published_context as f; print(f(__import__('sys').argv[1]).parameters)"
    result = run_code(code, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{'tail_bias_voltage': 1.2}"


def test_final_validation_live_has_guarded_resume_path():
    code = "import inspect,analog_opt.final_validation_live as m; s=inspect.getsource(m.verify_result); assert 'final_validation.confirmed.json' in s and '_final_tb_uses_result' in s and 'reuse_existing_final' in s"
    result = run_code(code)
    assert result.returncode == 0, result.stderr

def test_post_publication_corner_mapping_keeps_mixed_corner_mim_at_typical():
    code = "from analog_opt.live import patch_smic180_corner as f; M=lambda s:type('M',(),{'path':'models.scs','section':s})(); d=type('D',(),{'model_includes':[M('tt'),M('mim_tt')]})(); print([m.section for m in f(d,'FNSP',core_model_include='models.scs').model_includes])"
    result = run_code(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['fnsp', 'mim_tt']"

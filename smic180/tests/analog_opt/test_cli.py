import json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]; SCRIPT=ROOT/'skills'/'smic180-simulator'/'scripts'/'analog_optimize.py'
def config_data(): return {'version':2,'design':{'library':'tr','cell':'amp','work_cell':'amp_work','result_cell':'amp_opt','testbench_cell':'amp_tb'},'stimuli':{'VDD':{'kind':'voltage','value':'3.3V'}},'parameters':[],'analyses':[],'metrics':[],'specs':[],'search':{},'pvt':{},'outputs':{}}
def test_validate_is_offline_and_does_not_import_live_bridge(tmp_path):
    c=tmp_path/'config.json'; c.write_text(json.dumps(config_data())); (tmp_path/'sitecustomize.py').write_text("import sys\nclass B:\n def find_spec(self,n,p=None,t=None):\n  if n.startswith('sim_io'): raise RuntimeError('live import')\nsys.meta_path.insert(0,B())\n")
    env=dict(os.environ); env['PYTHONPATH']=str(tmp_path); r=subprocess.run([sys.executable,str(SCRIPT),'validate','--config',str(c)],text=True,capture_output=True,env=env); assert r.returncode==0,r.stderr; assert 'valid' in r.stdout.lower()
def test_validate_missing_config_has_path_error_exit_code(tmp_path):
    r=subprocess.run([sys.executable,str(SCRIPT),'validate','--config',str(tmp_path/'missing.json')],text=True,capture_output=True); assert r.returncode==2; assert 'missing.json' in r.stderr
def test_evaluate_rejects_nonfinite_candidate_before_live_factory(tmp_path):
    c=tmp_path/'config.json'; c.write_text(json.dumps(config_data())); p=tmp_path/'candidate.json'; p.write_text('{"W": NaN}'); r=subprocess.run([sys.executable,str(SCRIPT),'evaluate','--config',str(c),'--candidate',str(p)],text=True,capture_output=True); assert r.returncode==2; assert 'candidate' in r.stderr.lower()
def test_cli_exposes_five_commands():
    r=subprocess.run([sys.executable,str(SCRIPT),'--help'],text=True,capture_output=True); assert r.returncode==0
    for command in ('validate','evaluate','run','resume','report'): assert command in r.stdout
def test_report_is_offline(tmp_path):
    (tmp_path/'result_manifest.json').write_text(json.dumps({'best':{},'pvt':{},'failures':[],'artifacts':{}}))
    blocker=tmp_path/'sitecustomize.py'; blocker.write_text("import sys\nclass B:\n def find_spec(self,n,p=None,t=None):\n  if n.startswith('sim_io'): raise RuntimeError('live import')\nsys.meta_path.insert(0,B())\n")
    env=dict(os.environ); env['PYTHONPATH']=str(tmp_path)
    r=subprocess.run([sys.executable,str(SCRIPT),'report','--run-dir',str(tmp_path)],text=True,capture_output=True,env=env)
    assert r.returncode==0,r.stderr
    assert (tmp_path/'optimization_report.md').exists()

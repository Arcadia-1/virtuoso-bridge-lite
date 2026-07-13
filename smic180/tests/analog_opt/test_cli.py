import json,os,subprocess,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]; SCRIPT=ROOT/'skills'/'smic180-simulator'/'scripts'/'analog_optimize.py'
def config_data(): return {'version':2,'design':{'library':'tr','cell':'amp','work_cell':'amp_work','result_cell':'amp_opt','testbench_cell':'amp_tb'},'stimuli':{'VDD':{'kind':'voltage','value':'3.3V'}},'parameters':[],'analyses':[],'metrics':[],'specs':[],'search':{},'pvt':{},'outputs':{'run_dir':'run'}}
def run(*args,env=None): return subprocess.run([sys.executable,str(SCRIPT),*map(str,args)],text=True,capture_output=True,env=env)
def blocker(tmp_path):
 (tmp_path/'sitecustomize.py').write_text("import sys\nclass B:\n def find_spec(self,n,p=None,t=None):\n  if n in ('analog_opt.live','virtuoso_bridge') or n.startswith('sim_io'): raise RuntimeError('live import')\nsys.meta_path.insert(0,B())\n"); env=dict(os.environ); env['PYTHONPATH']=str(tmp_path); return env
def test_validate_and_report_are_offline(tmp_path):
 c=tmp_path/'config.json'; c.write_text(json.dumps(config_data())); r=run('validate','--config',c,env=blocker(tmp_path)); assert r.returncode==0,r.stderr
 (tmp_path/'result_manifest.json').write_text(json.dumps({'best':{},'pvt':{},'failures':[],'artifacts':{}})); r=run('report','--run-dir',tmp_path,env=blocker(tmp_path)); assert r.returncode==0,r.stderr
def test_evaluate_calls_live_workflow_evaluate(tmp_path):
 c=tmp_path/'config.json'; c.write_text(json.dumps(config_data())); p=tmp_path/'candidate.json'; p.write_text('{"X":1.0}')
 module=tmp_path/'fake_live.py'; module.write_text("class W:\n def evaluate(self,c):\n  open(r'%s','w').write(str(c)); return type('R',(),{'candidate_id':'evaluate','objective':1.0,'success':True,'metrics':{},'metadata':{},'failure':None,'specs':{}})()\ndef create_workflow(c,r): return W()\n"%(tmp_path/'called.txt'))
 env=dict(os.environ); env['PYTHONPATH']=str(tmp_path)+os.pathsep+str(ROOT/'skills'/'smic180-simulator'); env['ANALOG_OPT_LIVE_MODULE']='fake_live'
 r=run('evaluate','--config',c,'--candidate',p,env=env); assert r.returncode==0,r.stderr; assert (tmp_path/'called.txt').exists()
def test_live_error_returns_exit_3(tmp_path):
 c=tmp_path/'config.json'; c.write_text(json.dumps(config_data())); p=tmp_path/'candidate.json'; p.write_text('{}')
 module=tmp_path/'fake_live.py'; module.write_text("def create_workflow(c,r): raise RuntimeError('vm down')\n")
 env=dict(os.environ); env['PYTHONPATH']=str(tmp_path)+os.pathsep+str(ROOT/'skills'/'smic180-simulator'); env['ANALOG_OPT_LIVE_MODULE']='fake_live'
 r=run('evaluate','--config',c,'--candidate',p,env=env); assert r.returncode==3 and 'vm down' in r.stderr
def test_resume_reads_config_path_from_run_manifest(tmp_path):
 c=tmp_path/'resolved.json'; c.write_text(json.dumps(config_data())); (tmp_path/'run_manifest.json').write_text(json.dumps({'config':'resolved.json','artifacts':{}}))
 module=tmp_path/'fake_live.py'; module.write_text("class W:\n def resume(self): open(r'%s','w').write('ok')\ndef create_workflow(c,r): return W()\n"%(tmp_path/'resumed.txt'))
 env=dict(os.environ); env['PYTHONPATH']=str(tmp_path)+os.pathsep+str(ROOT/'skills'/'smic180-simulator'); env['ANALOG_OPT_LIVE_MODULE']='fake_live'
 r=run('resume','--run-dir',tmp_path,env=env); assert r.returncode==0,r.stderr; assert (tmp_path/'resumed.txt').exists()
def test_strict_candidate_and_path_errors_use_exit_2(tmp_path):
 c=tmp_path/'config.json'; c.write_text(json.dumps(config_data())); p=tmp_path/'candidate.json'; p.write_text('{"X":NaN}')
 assert run('evaluate','--config',c,'--candidate',p).returncode==2
 assert run('validate','--config',tmp_path/'missing.json').returncode==2
def test_cli_exposes_five_commands():
 r=run('--help'); assert r.returncode==0
 for name in ('validate','evaluate','run','resume','report'): assert name in r.stdout
def test_resume_rejects_manifest_config_path_outside_run_dir(tmp_path):
 outside=tmp_path.parent/'outside.json'; outside.write_text(json.dumps(config_data())); (tmp_path/'run_manifest.json').write_text(json.dumps({'config':'../outside.json','artifacts':{}}))
 r=run('resume','--run-dir',tmp_path)
 assert r.returncode==2 and 'config path' in r.stderr.lower()

import json
from pathlib import Path
import pytest
from analog_opt.evaluator import CandidateEvaluator, EvaluationFailure, EvaluationResult
from analog_opt.parameters import ParameterSpec
from analog_opt.pvt import PvtConfig
from analog_opt.search import SearchConfig, SearchResult
from analog_opt.workflow import AnalogSimulationBackend, OptimizationWorkflow

SPECS=[ParameterSpec('W','virtuoso_cdf',1e-6,20e-6,instance='M1',property='w',unit='m'),ParameterSpec('VBIAS','bias',.5,1.5,stimulus='VBIAS'),ParameterSpec('GAIN','spectre_variable',1.,10.,variable='gain')]
CANDIDATE={'W':10e-6,'VBIAS':1.2,'GAIN':4.}
class Log:
 def __init__(self): self.calls=[]
 def add(self,*x): self.calls.append(x)
class Applier:
 def __init__(self,log): self.log=log
 def create_work_cell(self,lib,src,work,replace): self.log.add('create',lib,src,work,replace)
 def apply_cdf(self,lib,cell,specs,candidate): self.log.add('apply',lib,cell,tuple(s.name for s in specs),dict(candidate))
 def read_cdf(self,lib,cell,specs): self.log.add('read',lib,cell); return {'W':10e-6}
 def publish_result_cell(self,lib,work,result,source,replace): self.log.add('publish',lib,work,result,source,replace)
class Netlist:
 def __init__(self,log,confirm=None): self.log=log; self.confirmed=confirm or {'gain':4.,'VBIAS':1.2,'VDD':3.3}
 def configure(self,design_variables,biases,stimuli,conditions): self.log.add('configure',dict(design_variables),dict(biases),dict(stimuli),dict(conditions))
 def export_fresh(self,library,work_cell,directory): self.log.add('export',library,work_cell,directory.name); return {'op':directory/'fresh.scs'}
 def confirm(self,path,expected_by_analysis): self.log.add('confirm',tuple(expected_by_analysis)); return {'op':dict(self.confirmed, dut_cell='amp_work')}
 def confirm_cdf(self,path,specs): return {'W':10e-6}
class Runner:
 def __init__(self,log,fail=False): self.log=log; self.fail=fail
 def run(self,path,directory,analyses): self.log.add('run',path.name)


def make_backend(tmp_path,log,confirm=None,spec_summary=None,runner=None):
 r=runner or Runner(log); r.run=lambda p,d,a: (_ for _ in ()).throw(RuntimeError('spectre')) if getattr(r,'fail',False) else (log.add('run',next(iter(p.values())).name) or {'raw':1})
 return AnalogSimulationBackend('tr','amp_work',SPECS,{'VDD':{'value':3.3,'optimizable':False},'VBIAS':{'value':1.,'optimizable':True}},[{'name':'op','type':'dc_op'}],[{'metric':'gain'}],applier=Applier(log),netlist=Netlist(log,confirm),runner=r,metric_extractor=lambda raw: log.add('metrics') or {'gain':8.},spec_evaluator=lambda metrics: spec_summary or {'objective':.25,'passed':False,'results':{'gain':{'passed':False,'violation':.25}}})

def test_backend_uses_real_applier_signature_and_structured_confirmation(tmp_path):
 log=Log(); result=make_backend(tmp_path,log)(CANDIDATE,tmp_path)
 assert result['success'] is True and result['objective']==.25
 assert log.calls[0]==('apply','tr','amp_work',('W',),{'W':10e-6})
 assert log.calls[1][0]=='configure' and log.calls[1][1:]==({'gain':4.},{'VBIAS':1.2},{'VDD':{'value':3.3,'optimizable':False}},{})
 assert [c[0] for c in log.calls]==['apply','configure','export','run','metrics','read','confirm']

def test_backend_confirmation_compares_each_finite_physical_value(tmp_path):
 log=Log(); backend=make_backend(tmp_path,log,confirm={'gain':4.,'VBIAS':1.19,'VDD':3.3})
 with pytest.raises(EvaluationFailure) as err: backend(CANDIDATE,tmp_path)
 assert err.value.category=='confirmation' and 'VBIAS' in err.value.message

def test_backend_classifies_each_stage(tmp_path):
 stages={}
 for stage in ('apply','netlist','simulation','metrics','specification'):
  log=Log(); backend=make_backend(tmp_path,log)
  if stage=='apply': backend.applier.apply_cdf=lambda *a: (_ for _ in ()).throw(RuntimeError('x'))
  if stage=='netlist': backend.netlist.export_fresh=lambda *a: (_ for _ in ()).throw(RuntimeError('x'))
  if stage=='simulation': backend.runner.run=lambda *a: (_ for _ in ()).throw(RuntimeError('x'))
  if stage=='metrics': backend.metric_extractor=lambda *a: (_ for _ in ()).throw(RuntimeError('x'))
  if stage=='specification': backend.spec_evaluator=lambda *a: {'objective':float('nan'),'passed':True,'results':{}}
  with pytest.raises(EvaluationFailure) as err: backend(CANDIDATE,tmp_path)
  stages[stage]=err.value.category
 assert stages=={x:x for x in stages}

def test_backend_rejects_fixed_stimulus_optimization(tmp_path):
 specs=[ParameterSpec('VDD','bias',2.,4.,stimulus='VDD')]
 with pytest.raises(EvaluationFailure,match='fixed stimulus'):
  AnalogSimulationBackend('tr','w',specs,{'VDD':{'value':3.3,'optimizable':False}},[],[],applier=Applier(Log()),netlist=Netlist(Log()),runner=Runner(Log()),metric_extractor=lambda x:{},spec_evaluator=lambda x:{'objective':0.,'passed':True,'results':{}})({'VDD':3.},tmp_path)

class WorkflowApplier(Applier):
 def __init__(self,log): super().__init__(log); self.published=False
 def publish_result_cell(self,lib,work,result,source,replace): super().publish_result_cell(lib,work,result,source,replace); self.published=True
 def confirm_result_cell(self,lib,result,source_hash): self.log.add('confirm_result',lib,result,source_hash); return self.published

def eval_result(cid='best-replay',passed=True,params=CANDIDATE):
 return EvaluationResult(cid,.1,True,{'measured':{'gain':8.},'derived':{},'unavailable':{}},{'physical_candidate':dict(params)},None,{'gain':{'passed':passed,'violation':0. if passed else .2}})

def workflow(tmp_path,log,search_result=None,pvt_pass=True,applier=None):
 best=eval_result('candidate-000000')
 return OptimizationWorkflow(tmp_path,library='tr',source_cell='amp',work_cell='amp_work',result_cell='amp_opt',parameter_specs=SPECS,applier=applier or WorkflowApplier(log),evaluator=object(),search_config=SearchConfig('random',1,1),search_runner=lambda resume: log.add('search',resume) or (search_result or SearchResult((best,),best,False)),replay=lambda candidate,directory,conditions=None: log.add('replay',dict(candidate),directory.name,conditions) or eval_result(params=candidate),pvt_config=PvtConfig(('TT',),(3.3,),(25.,)),pvt_evaluator=lambda point,candidate,directory: log.add('pvt_eval',point.point_id,directory.name) or eval_result(point.point_id,pvt_pass,candidate))

def test_workflow_evaluate_uses_candidate_evaluator_once(tmp_path):
 class E:
  def __init__(self): self.calls=[]
  def evaluate(self,run_dir,cid,candidate): self.calls.append((Path(run_dir),cid,dict(candidate))); return eval_result(cid,params=candidate)
 e=E(); w=workflow(tmp_path,Log()); w.evaluator=e
 result=w.evaluate(CANDIDATE)
 assert result.success and len(e.calls)==1 and e.calls[0][2]==CANDIDATE

def test_workflow_full_order_fresh_replay_pvt_report_publish(tmp_path):
 log=Log(); w=workflow(tmp_path,log); state=w.run(replace_work_cell=True,replace_result_cell=True)
 names=[c[0] for c in log.calls]
 assert names==['create','search','replay','pvt_eval','confirm_result','publish','confirm_result']
 assert state['state']=='published'
 assert (tmp_path/'run_manifest.json').exists() and (tmp_path/'result_manifest.json').exists() and (tmp_path/'optimization_report.md').exists()
 assert json.loads((tmp_path/'pvt_results.json').read_text())['overall_passed'] is True

def test_fresh_best_must_pass_specs_and_match_parameters(tmp_path):
 log=Log(); w=workflow(tmp_path,log); w.replay=lambda c,d,conditions=None: eval_result(passed=False,params=c)
 with pytest.raises(EvaluationFailure) as err: w.run()
 assert err.value.category=='best_replay' and not any(c[0]=='publish' for c in log.calls)

def test_pvt_failure_reports_without_publish(tmp_path):
 log=Log(); w=workflow(tmp_path,log,pvt_pass=False); state=w.run()
 assert state['state']=='reported' and not any(c[0]=='publish' for c in log.calls)
 assert (tmp_path/'result_manifest.json').exists()

def test_searching_resume_nonrandom_is_rejected(tmp_path):
 (tmp_path/'workflow_state.json').write_text(json.dumps({'version':1,'state':'searching','parameter_names':['GAIN','VBIAS','W']}))
 w=workflow(tmp_path,Log()); w.search_config=SearchConfig('scipy',1,1)
 with pytest.raises(EvaluationFailure,match='cannot resume'): w.resume()

def test_publishing_resume_confirms_then_retries_safely(tmp_path):
 log=Log(); a=WorkflowApplier(log); w=workflow(tmp_path,log,applier=a)
 intent={'candidate_hash':'abc','parameters':CANDIDATE}; (tmp_path/'publication.json').write_text(json.dumps(intent)); (tmp_path/'workflow_state.json').write_text(json.dumps({'version':1,'state':'publishing','parameter_names':['GAIN','VBIAS','W'],'parameters':CANDIDATE,'candidate_hash':'abc','pvt':{'overall_passed':True},'best':{'objective':.1,'metrics':{},'specs':{'gain':{'passed':True,'violation':0.}},'parameters':CANDIDATE}}))
 state=w.resume(replace_result_cell=True)
 assert [c[0] for c in log.calls]==['confirm_result','publish','confirm_result'] and state['state']=='published'

def test_state_rejects_nonfinite_or_missing_required_fields(tmp_path):
 (tmp_path/'workflow_state.json').write_text('{"version":1,"state":"best_replayed","objective":NaN}')
 with pytest.raises(EvaluationFailure) as err: workflow(tmp_path,Log()).resume()
 assert err.value.category=='state'
def test_state_requires_best_pvt_and_publication_fields(tmp_path):
 base={'version':1,'parameter_names':['GAIN','VBIAS','W'],'parameters':CANDIDATE}
 for state in ('best_replayed','pvt_validated','reported','publishing'):
  (tmp_path/'workflow_state.json').write_text(json.dumps(dict(base,state=state)))
  with pytest.raises(EvaluationFailure) as err: workflow(tmp_path,Log()).resume()
  assert err.value.category=='state'

def test_hard_spec_failure_has_penalized_objective_and_never_publishes(tmp_path):
 log=Log(); best=eval_result('candidate-000000'); best=EvaluationResult(best.candidate_id,10000.2,True,best.metrics,{'physical_candidate':dict(CANDIDATE)},None,{'gain':{'passed':False,'violation':.2}})
 w=workflow(tmp_path,log,search_result=SearchResult((best,),best,False))
 w.replay=lambda candidate,directory,conditions=None: EvaluationResult('best-replay',10000.2,True,{}, {'physical_candidate':dict(candidate)},None,{'gain':{'passed':False,'violation':.2}})
 with pytest.raises(EvaluationFailure) as err: w.run()
 assert err.value.category=='best_replay' and not any(c[0]=='publish' for c in log.calls)

def test_backend_confirms_every_analysis_deck_and_dc_parameter_token(tmp_path):
 log=Log(); backend=make_backend(tmp_path,log)
 backend.analyses=({'name':'line','type':'dc_sweep','source':'VDD','parameter':'VDD_SWEEP','start':2.7,'stop':3.6,'points':3},{'name':'ac_main','type':'ac','start':1.,'stop':1e6,'points_per_decade':10})
 class MultiNet(Netlist):
  def export_fresh(self,library,cell,directory): return {'line':directory/'line.scs','ac_main':directory/'ac.scs'}
  def confirm_cdf(self,decks,specs): return {'W':10e-6}
  def confirm(self,decks,expected):
   self.log.add('confirm_multi',decks,expected)
   return {'line':{'VDD_SWEEP':'VDD_SWEEP','VBIAS':1.2,'gain':4.0,'dut_cell':'amp_work'},'ac_main':{'VDD':3.3,'VBIAS':1.2,'gain':4.0,'dut_cell':'amp_work'}}
 backend.netlist=MultiNet(log); backend.runner.run=lambda decks,d,a:{name:type('R',(),{'ok':True,'data':{}})() for name in decks}; backend.metric_extractor=lambda r:{}
 result=backend(CANDIDATE,tmp_path)
 assert result['success'] is True and any(c[0]=='confirm_multi' for c in log.calls)

def test_backend_fails_when_one_analysis_confirmation_is_missing(tmp_path):
 backend=make_backend(tmp_path,Log()); backend.analyses=({'name':'a','type':'ac'},{'name':'b','type':'ac'})
 class N(Netlist):
  def export_fresh(self,l,c,d): return {'a':d/'a.scs','b':d/'b.scs'}
  def confirm_cdf(self,d,s): return {'W':10e-6}
  def confirm(self,d,e): return {'a':{'VDD':3.3,'VBIAS':1.2,'gain':4.0,'dut_cell':'amp_work'}}
 backend.netlist=N(Log()); backend.runner.run=lambda d,p,a:{k:type('R',(),{'ok':True,'data':{}})() for k in d}; backend.metric_extractor=lambda r:{}
 with pytest.raises(EvaluationFailure) as err: backend(CANDIDATE,tmp_path)
 assert err.value.category=='confirmation' and 'analysis' in err.value.message

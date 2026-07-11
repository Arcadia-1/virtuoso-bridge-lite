import json
import pytest
from analog_opt.evaluator import EvaluationFailure, EvaluationResult
from analog_opt.parameters import ParameterSpec
from analog_opt.workflow import AnalogSimulationBackend, OptimizationWorkflow
class Calls:
    def __init__(self): self.items=[]
    def add(self,name,*args): self.items.append((name,args))
def test_backend_partitions_values_and_confirms_fresh_netlist(tmp_path):
    calls=Calls(); specs=[ParameterSpec('W','virtuoso_cdf',1e-6,20e-6,instance='M1',property='w',unit='m'),ParameterSpec('VBIAS','bias',.5,1.5,stimulus='VBIAS'),ParameterSpec('GAIN','spectre_variable',1.,10.,variable='gain')]
    stimuli={'VDD':{'value':3.3,'optimizable':False},'VBIAS':{'value':1.,'optimizable':True}}
    class A:
        def apply_cdf(self,v): calls.add('apply',v)
        def read_cdf(self,v): calls.add('read',v); return {'W':10e-6}
    class N:
        def export_fresh(self,d): calls.add('netlist',d); return d/'fresh.scs'
        def confirm_values(self,p,e): calls.add('confirm',p,e); return True
    class R:
        def run(self,p,d,a): calls.add('run',p,a); return {'raw':1}
    backend=AnalogSimulationBackend(specs,stimuli,[{'name':'op','type':'dc_op'}],[],applier=A(),netlist=N(),runner=R(),metric_extractor=lambda raw:calls.add('metrics',raw) or {'gain':8.},spec_evaluator=lambda m:calls.add('specs',m) or {'objective':0.,'passed':True,'results':{}})
    result=backend({'W':10e-6,'VBIAS':1.2,'GAIN':4.},tmp_path)
    assert result['success'] is True
    assert [n for n,_ in calls.items]==['apply','netlist','run','metrics','specs','read','confirm']
    assert calls.items[-1][1][1]=={'gain':4.,'VBIAS':1.2,'VDD':3.3}
def test_backend_rejects_fixed_stimulus_parameter(tmp_path):
    spec=ParameterSpec('VDD','bias',2.5,3.6,stimulus='VDD')
    backend=AnalogSimulationBackend([spec],{'VDD':{'value':3.3,'optimizable':False}},[],[],applier=object(),netlist=object(),runner=object(),metric_extractor=lambda r:{},spec_evaluator=lambda m:{})
    with pytest.raises(EvaluationFailure,match='fixed stimulus'): backend({'VDD':3.},tmp_path)
def test_workflow_replays_best_fresh_and_publishes_after_report(tmp_path):
    calls=Calls(); best=EvaluationResult('candidate-000001',.1,True,{'gain':9.},{'physical_candidate':{'W':2.}},None,{})
    class A:
        def create_work_cell(self,replace=False): calls.add('create',replace)
        def publish_result_cell(self,replace=False): calls.add('publish',replace)
    w=OptimizationWorkflow(tmp_path,applier=A(),search=lambda resume:calls.add('search',resume) or type('R',(),{'best':best})(),replay=lambda c,d:calls.add('replay',c,d) or best,validate_pvt=lambda c:calls.add('pvt',c) or {'overall_passed':True},reporter=lambda d:calls.add('report',d))
    w.run(replace_work_cell=True,replace_result_cell=True)
    assert [n for n,_ in calls.items]==['create','search','replay','pvt','report','publish']; assert calls.items[2][1][0]=={'W':2.}; assert calls.items[2][1][1].name=='best_replay'; assert json.loads((tmp_path/'workflow_state.json').read_text())['state']=='published'
def test_workflow_pvt_failure_reports_but_does_not_publish(tmp_path):
    calls=Calls(); best=EvaluationResult('candidate-000001',.1,True,{}, {'physical_candidate':{'W':2.}},None,{})
    class A:
        def create_work_cell(self,replace=False): calls.add('create')
        def publish_result_cell(self,replace=False): calls.add('publish')
    w=OptimizationWorkflow(tmp_path,applier=A(),search=lambda r:type('R',(),{'best':best})(),replay=lambda c,d:best,validate_pvt=lambda c:{'overall_passed':False},reporter=lambda d:calls.add('report')); w.run()
    assert [n for n,_ in calls.items]==['create','report']; assert json.loads((tmp_path/'workflow_state.json').read_text())['state']=='reported'
def test_resume_retries_incomplete_transition_without_skipping(tmp_path):
    (tmp_path/'workflow_state.json').write_text(json.dumps({'state':'searching'})); calls=Calls(); best=EvaluationResult('candidate-000001',.1,True,{}, {'physical_candidate':{'W':2.}},None,{})
    class A:
        def create_work_cell(self,replace=False): calls.add('create')
        def publish_result_cell(self,replace=False): calls.add('publish')
    w=OptimizationWorkflow(tmp_path,applier=A(),search=lambda r:calls.add('search',r) or type('R',(),{'best':best})(),replay=lambda c,d:calls.add('replay') or best,validate_pvt=lambda c:calls.add('pvt') or {'overall_passed':True},reporter=lambda d:calls.add('report')); w.resume()
    assert calls.items[0]==('search',(True,)); assert [n for n,_ in calls.items]==['search','replay','pvt','report','publish']
def test_backend_categorizes_runner_error(tmp_path):
    spec=ParameterSpec('GAIN','spectre_variable',1.,10.,variable='gain')
    class N:
        def export_fresh(self,d): return d/'fresh.scs'
        def confirm_values(self,p,e): return True
    class R:
        def run(self,p,d,a): raise RuntimeError('spectre failed')
    backend=AnalogSimulationBackend([spec],{},[],[],applier=object(),netlist=N(),runner=R(),metric_extractor=lambda r:{},spec_evaluator=lambda m:{})
    with pytest.raises(EvaluationFailure) as error: backend({'GAIN':4.},tmp_path)
    assert error.value.category=='simulation'

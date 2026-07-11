"""Strict injected backend and recoverable analog optimization workflow."""
from __future__ import annotations
import hashlib,json,math
from dataclasses import asdict,is_dataclass
from pathlib import Path
from typing import Any,Callable,Mapping,Sequence
from analog_opt.evaluator import CandidateEvaluator,EvaluationFailure,EvaluationResult,atomic_write_json
from analog_opt.parameters import ParameterSpec
from analog_opt.pvt import build_pvt_points,pvt_result_from_evaluation,summarize_pvt
from analog_opt.report import write_pvt_results,write_report,write_result_manifest,write_run_manifest

_STATES=('validated','work_cell_created','searching','best_replayed','pvt_validated','reported','publishing','published')
def _finite(value,label):
 if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(float(value)): raise EvaluationFailure('state','%s must be finite'%label)
 return float(value)
def _plain(value): return asdict(value) if is_dataclass(value) else dict(value)
def _stimulus(value):
 if isinstance(value,Mapping): return value.get('value',value.get('dc')),value.get('optimizable',False)
 raw=getattr(value,'value',None); return raw if raw is not None else getattr(value,'dc',None),getattr(value,'optimizable',False)
def _hash(candidate): return hashlib.sha256(json.dumps(candidate,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def _spec_protocol(summary):
 if is_dataclass(summary): summary=asdict(summary)
 if not isinstance(summary,Mapping): raise ValueError('spec evaluator must return mapping')
 objective=summary.get('objective',summary.get('total'))
 if isinstance(objective,bool) or not isinstance(objective,(int,float)) or not math.isfinite(float(objective)): raise ValueError('spec objective must be finite')
 if type(summary.get('passed')) is not bool or not isinstance(summary.get('results'),Mapping): raise ValueError('spec summary protocol is invalid')
 results={}
 for name,item in summary['results'].items():
  if is_dataclass(item): item=asdict(item)
  if not isinstance(item,Mapping) or type(item.get('passed')) is not bool: raise ValueError('spec result protocol is invalid')
  violation=item.get('violation',0.)
  if isinstance(violation,bool) or not isinstance(violation,(int,float)) or not math.isfinite(float(violation)) or violation<0: raise ValueError('spec violation must be finite and nonnegative')
  results[str(name)]={'passed':item['passed'],'violation':float(violation)}
 return float(objective),summary['passed'],results

class AnalogSimulationBackend:
 def __init__(self,library:str,work_cell:str,parameter_specs:Sequence[ParameterSpec],stimuli:Mapping[str,Any],analyses:Sequence[Mapping[str,Any]],specs:Sequence[Any],*,applier:Any,netlist:Any,runner:Any,metric_extractor:Callable,spec_evaluator:Callable,confirmation_rtol:float=1e-9,confirmation_atol:float=1e-15):
  self.library=library; self.work_cell=work_cell; self.parameter_specs=tuple(parameter_specs); self.stimuli=dict(stimuli); self.analyses=tuple(analyses); self.specs=tuple(specs); self.applier=applier; self.netlist=netlist; self.runner=runner; self.metric_extractor=metric_extractor; self.spec_evaluator=spec_evaluator; self.rtol=confirmation_rtol; self.atol=confirmation_atol
 def __call__(self,candidate:Mapping[str,Any],directory:Path,conditions:Mapping[str,Any]=None):
  expected={s.name for s in self.parameter_specs}
  if not isinstance(candidate,Mapping) or set(candidate)!=expected: raise EvaluationFailure('candidate','candidate parameters must exactly match configuration')
  cdf_specs=[]; cdf={}; variables={}; biases={}
  for spec in self.parameter_specs:
   value=candidate[spec.name]
   if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(float(value)): raise EvaluationFailure('candidate','parameter %s must be finite'%spec.name)
   if spec.target=='virtuoso_cdf': cdf_specs.append(spec); cdf[spec.name]=value
   elif spec.target=='spectre_variable': variables[spec.variable or spec.name]=value
   elif spec.target=='bias':
    name=spec.stimulus or spec.name
    if name not in self.stimuli or _stimulus(self.stimuli[name])[1] is not True: raise EvaluationFailure('configuration','fixed stimulus cannot be optimized: %s'%name)
    biases[name]=value
   else: raise EvaluationFailure('configuration','unsupported parameter target: %s'%spec.target)
  fixed={name:value for name,item in self.stimuli.items() for value,opt in [_stimulus(item)] if not opt and value is not None}
  try:
   if cdf_specs: self.applier.apply_cdf(self.library,self.work_cell,cdf_specs,cdf)
  except Exception as exc: raise EvaluationFailure('apply',str(exc)) from exc
  try:
   self.netlist.configure(variables,biases,fixed,conditions or {})
   deck=self.netlist.export_fresh(self.library,self.work_cell,Path(directory))
  except Exception as exc: raise EvaluationFailure('netlist',str(exc)) from exc
  try: raw=self.runner.run(deck,Path(directory),self.analyses)
  except Exception as exc: raise EvaluationFailure('simulation',str(exc)) from exc
  try: metrics=dict(self.metric_extractor(raw))
  except Exception as exc: raise EvaluationFailure('metrics',str(exc)) from exc
  try: objective,passed,spec_results=_spec_protocol(self.spec_evaluator(metrics))
  except Exception as exc: raise EvaluationFailure('specification',str(exc)) from exc
  try:
   observed={}
   if cdf_specs: observed.update(self.applier.read_cdf(self.library,self.work_cell,cdf_specs))
   names=list(variables)+list(biases)+list(fixed); net_values=self.netlist.confirm(deck,names)
   if not isinstance(net_values,Mapping): raise ValueError('netlist confirmation must return mapping')
   observed.update(net_values); requested=dict(cdf); requested.update(variables); requested.update(biases); requested.update(fixed)
   for name,want in requested.items():
    if name not in observed: raise ValueError('missing confirmation for %s'%name)
    got=observed[name]
    if isinstance(got,bool) or not isinstance(got,(int,float)) or not math.isfinite(float(got)) or not math.isclose(float(got),float(want),rel_tol=self.rtol,abs_tol=self.atol): raise ValueError('%s physical value mismatch'%name)
  except Exception as exc: raise EvaluationFailure('confirmation',str(exc)) from exc
  return {'objective':objective,'success':True,'metrics':metrics,'specs':spec_results,'metadata':{'physical_candidate':dict(candidate),'specs_passed':passed,'netlist':str(deck)}}

class OptimizationWorkflow:
 def __init__(self,run_dir:Any,*,library:str,source_cell:str,work_cell:str,result_cell:str,parameter_specs:Sequence[ParameterSpec],applier:Any,evaluator:Any,search_config:Any,search_runner:Callable,replay:Callable,pvt_config:Any,pvt_evaluator:Callable):
  self.run_dir=Path(run_dir); self.run_dir.mkdir(parents=True,exist_ok=True); self.library=library; self.source_cell=source_cell; self.work_cell=work_cell; self.result_cell=result_cell; self.parameter_specs=tuple(parameter_specs); self.applier=applier; self.evaluator=evaluator; self.search_config=search_config; self.search_runner=search_runner; self.replay=replay; self.pvt_config=pvt_config; self.pvt_evaluator=pvt_evaluator; self.state_path=self.run_dir/'workflow_state.json'
 @property
 def names(self): return tuple(sorted(s.name for s in self.parameter_specs))
 def _save(self,state,**data): atomic_write_json(self.state_path,dict(data,version=1,state=state,parameter_names=list(self.names)))
 def _load(self):
  if not self.state_path.exists(): return None
  try: data=json.loads(self.state_path.read_text(encoding='utf-8'),parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
  except Exception as exc: raise EvaluationFailure('state','invalid workflow state: %s'%exc) from exc
  if not isinstance(data,Mapping) or data.get('version')!=1 or data.get('state') not in _STATES or data.get('parameter_names')!=list(self.names): raise EvaluationFailure('state','workflow state schema is invalid')
  if data['state'] in ('best_replayed','pvt_validated','reported','publishing','published'):
   self._candidate(data.get('parameters'))
   best=data.get('best')
   if not isinstance(best,Mapping) or not isinstance(best.get('metrics'),Mapping) or not isinstance(best.get('specs'),Mapping) or not best['specs']:
    raise EvaluationFailure('state','workflow best replay state is incomplete')
   _finite(best.get('objective'),'best objective')
  if data['state'] in ('pvt_validated','reported','publishing','published') and not isinstance(data.get('pvt'),Mapping):
   raise EvaluationFailure('state','workflow PVT state is incomplete')
  if data['state'] in ('publishing','published'):
   if not isinstance(data.get('candidate_hash'),str) or len(data['candidate_hash'])<3: raise EvaluationFailure('state','workflow publication state is incomplete')
   try: intent=json.loads((self.run_dir/'publication.json').read_text(encoding='utf-8'))
   except Exception as exc: raise EvaluationFailure('state','publication intent is missing') from exc
   if intent.get('candidate_hash')!=data['candidate_hash'] or intent.get('parameters')!=data['parameters']: raise EvaluationFailure('state','publication intent does not match state')
  return dict(data)
 def _candidate(self,candidate):
  if not isinstance(candidate,Mapping) or set(candidate)!=set(self.names) or not candidate: raise EvaluationFailure('candidate','best parameters must be nonempty and exactly match configuration')
  for name,value in candidate.items():
   if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(float(value)): raise EvaluationFailure('candidate','parameter %s must be finite'%name)
  return dict(candidate)
 def evaluate(self,candidate): return self.evaluator.evaluate(self.run_dir,'evaluate',self._candidate(candidate))
 def _search_best_parameters(self,result):
  if result.best is None: raise EvaluationFailure('search','search produced no successful candidate')
  history=json.loads((self.run_dir/'search_history.json').read_text(encoding='utf-8')) if (self.run_dir/'search_history.json').exists() else None
  if history:
   matches=[x for x in history['history'] if x.get('candidate_id')==result.best.candidate_id]
   if len(matches)!=1: raise EvaluationFailure('search','best candidate record is incomplete')
   return self._candidate(matches[0].get('physical_candidate'))
  return self._candidate(result.best.metadata.get('physical_candidate'))
 def run(self,replace_work_cell=False,replace_result_cell=False):
  if self.state_path.exists(): raise EvaluationFailure('state','run directory already has workflow state; use resume')
  self._save('validated'); return self._execute(False,replace_work_cell,replace_result_cell)
 def resume(self,replace_result_cell=False): return self._execute(True,False,replace_result_cell)
 def _execute(self,resume,replace_work,replace_result):
  data=self._load()
  if data is None: raise EvaluationFailure('state','resume requires workflow state')
  state=data['state']
  if state=='validated': self.applier.create_work_cell(self.library,self.source_cell,self.work_cell,replace_work); self._save('work_cell_created'); state='work_cell_created'
  if state in ('work_cell_created','searching'):
   if state=='searching' and self.search_config.method!='random': raise EvaluationFailure('search','non-random search cannot resume without a complete best candidate')
   self._save('searching'); search=self.search_runner(state=='searching'); parameters=self._search_best_parameters(search)
   replay=self.replay(parameters,self.run_dir/'best_replay',None); self._validate_replay(replay,parameters)
   best={'objective':float(replay.objective),'metrics':dict(replay.metrics),'specs':dict(replay.specs),'parameters':parameters}
   self._save('best_replayed',parameters=parameters,best=best); data=self._load(); state='best_replayed'
  parameters=data.get('parameters'); best=data.get('best')
  if state=='best_replayed':
   points=build_pvt_points(self.pvt_config); results=[]
   for point in points:
    directory=self.run_dir/'pvt'/point.point_id
    result=self.pvt_evaluator(point,parameters,directory)
    results.append(pvt_result_from_evaluation(point,result,parameters))
   summary=summarize_pvt(points,results,expected_spec_ids=tuple(best['specs']))
   write_pvt_results(self.run_dir,summary); pvt=_plain(summary); self._save('pvt_validated',parameters=parameters,best=best,pvt=pvt); data=self._load(); state='pvt_validated'
  pvt=data.get('pvt')
  if state=='pvt_validated':
   payload=self._report_payload(best,pvt); write_run_manifest(self.run_dir,{'config':'analog_opt_config.resolved.json','state':'reported','artifacts':payload['artifacts']}); write_result_manifest(self.run_dir,payload); write_report(self.run_dir,payload); self._save('reported',parameters=parameters,best=best,pvt=pvt); state='reported'
  if state=='reported' and pvt.get('overall_passed') is True:
   candidate_hash=_hash(parameters); atomic_write_json(self.run_dir/'publication.json',{'candidate_hash':candidate_hash,'parameters':parameters}); self._save('publishing',parameters=parameters,best=best,pvt=pvt,candidate_hash=candidate_hash); state='publishing'
  if state=='publishing':
   data=self._load(); candidate_hash=data['candidate_hash']
   if not self.applier.confirm_result_cell(self.library,self.result_cell,candidate_hash): self.applier.publish_result_cell(self.library,self.work_cell,self.result_cell,self.source_cell,replace_result)
   if self.applier.confirm_result_cell(self.library,self.result_cell,candidate_hash) is not True: raise EvaluationFailure('publication','result cell publication could not be confirmed')
   self._save('published',parameters=parameters,best=best,pvt=pvt,candidate_hash=candidate_hash)
  return self._load()
 def _validate_replay(self,result,parameters):
  if not isinstance(result,EvaluationResult) or result.success is not True or not math.isfinite(float(result.objective)): raise EvaluationFailure('best_replay','fresh best replay failed')
  actual=result.metadata.get('physical_candidate') if isinstance(result.metadata,Mapping) else None
  if actual!=parameters or not result.specs or not all(isinstance(v,Mapping) and v.get('passed') is True for v in result.specs.values()): raise EvaluationFailure('best_replay','fresh best replay did not confirm passing specifications and parameters')
 def _report_payload(self,best,pvt):
  artifacts={'best_replay':'best_replay','pvt_results':'pvt_results.json','report':'optimization_report.md'}
  return {'best':best,'pvt':pvt,'failures':[],'artifacts':artifacts}
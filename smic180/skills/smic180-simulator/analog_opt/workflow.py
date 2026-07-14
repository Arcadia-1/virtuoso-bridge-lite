"""Strict injected backend and recoverable analog optimization workflow."""
from __future__ import annotations
import hashlib,json,math
from dataclasses import asdict,is_dataclass
from pathlib import Path
from typing import Any,Callable,Mapping,Sequence
from analog_opt.evaluator import CandidateEvaluator,EvaluationFailure,EvaluationResult,atomic_write_json
from analog_opt.parameters import ParameterSpec
from analog_opt.profiles import profile_summary_hash
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
def _profile_candidate_hash(candidate): return _hash({str(name):float(value) for name,value in candidate.items()})
def _deck_hash(deck):
 items=deck.items() if isinstance(deck,Mapping) else (("default",deck),)
 hashes={}
 for name,value in items:
  path=Path(value)
  if not path.is_file(): raise ValueError('fresh netlist is missing: '+str(path))
  hashes[str(name)]=hashlib.sha256(path.read_bytes()).hexdigest()
 return _hash(hashes)
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
  fixed={name:item for name,item in self.stimuli.items() if _stimulus(item)[1] is not True and _stimulus(item)[0] is not None}
  try:
   if cdf_specs: self.applier.apply_cdf(self.library,self.work_cell,cdf_specs,cdf)
  except Exception as exc: raise EvaluationFailure('apply',str(exc)) from exc
  try:
   self.netlist.configure(variables,biases,self.stimuli,conditions or {})
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
   if cdf_specs:
    observed.update(self.applier.read_cdf(self.library,self.work_cell,cdf_specs))
    deck_cdf=self.netlist.confirm_cdf(deck,cdf_specs)
    specs_by_name={spec.name:spec for spec in cdf_specs}
    for name,value in deck_cdf.items():
     if name not in observed or math.isclose(float(observed[name]),float(value),rel_tol=self.rtol,abs_tol=self.atol): continue
     raise ValueError('%s final deck CDF mismatch'%name)
   base_requested=dict(variables); base_requested.update(biases); base_requested.update({name:_stimulus(item)[0] for name,item in fixed.items()}); base_requested['dut_cell']=self.work_cell
   if conditions:
    if 'temperature' in conditions: base_requested['temperature']=conditions['temperature']
    if 'corner' in conditions: base_requested['corner']=conditions['corner']
    if conditions.get('voltage') is not None: base_requested[conditions['voltage_stimulus']]=conditions['voltage']
   expected_by_analysis={}
   for analysis in self.analyses:
    requested=dict(base_requested)
    if analysis.get('type')=='dc_sweep':
     source=analysis['source']; requested.pop(source,None); requested[analysis['parameter']]=analysis['parameter']
    expected_by_analysis[analysis['name']]=requested
   net_values=self.netlist.confirm(deck,expected_by_analysis)
   if not isinstance(net_values,Mapping) or set(net_values)!=set(expected_by_analysis): raise ValueError('analysis confirmation set is incomplete')
   for analysis_name,requested in expected_by_analysis.items():
    actual=net_values.get(analysis_name)
    if not isinstance(actual,Mapping): raise ValueError('analysis confirmation missing: '+analysis_name)
    for name,want in requested.items():
     if name not in actual: raise ValueError('analysis %s missing confirmation for %s'%(analysis_name,name))
     got=actual[name]
     if isinstance(want,str):
      matches=str(got).lower()==want.lower() if name=='corner' else got==want
      if not matches: raise ValueError('analysis %s physical value mismatch for %s'%(analysis_name,name))
     elif isinstance(got,bool) or not isinstance(got,(int,float)) or not math.isfinite(float(got)) or not math.isclose(float(got),float(want),rel_tol=self.rtol,abs_tol=self.atol): raise ValueError('analysis %s physical value mismatch for %s'%(analysis_name,name))
  except Exception as exc: raise EvaluationFailure('confirmation',str(exc)) from exc
  return {'objective':objective,'success':True,'metrics':metrics,'specs':spec_results,'metadata':{'physical_candidate':dict(candidate),'specs_passed':passed,'netlist':str(deck),'testbench_signature':_hash(net_values),'netlist_hash':_deck_hash(deck),'measurement_hash':_hash(metrics),'artifacts':dict(metrics.get('artifacts',{})) if isinstance(metrics,Mapping) else {}}}

class OptimizationWorkflow:
 def __init__(self,run_dir:Any,*,config_payload:Mapping[str,Any]=None,library:str,source_cell:str,work_cell:str,result_cell:str,parameter_specs:Sequence[ParameterSpec],applier:Any,evaluator:Any,search_config:Any,search_runner:Callable,replay:Callable,pvt_config:Any,pvt_evaluator:Callable):
  self.run_dir=Path(run_dir); self.run_dir.mkdir(parents=True,exist_ok=True); self.config_payload=dict(config_payload or {}); self.library=library; self.source_cell=source_cell; self.work_cell=work_cell; self.result_cell=result_cell; self.parameter_specs=tuple(parameter_specs); self.applier=applier; self.evaluator=evaluator; self.search_config=search_config; self.search_runner=search_runner; self.replay=replay; self.pvt_config=pvt_config; self.pvt_evaluator=pvt_evaluator; self.state_path=self.run_dir/'workflow_state.json'
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
   if self._requires_profile_evidence() and (intent.get('profile_summary_hash')!=data.get('profile_summary_hash') or data.get('profile_summary_hash')!=data['best'].get('profile_summary_hash')): raise EvaluationFailure('state','publication profile summary hash does not match state')
  return dict(data)
 def _candidate(self,candidate):
  if not isinstance(candidate,Mapping) or set(candidate)!=set(self.names) or not candidate: raise EvaluationFailure('candidate','best parameters must be nonempty and exactly match configuration')
  for name,value in candidate.items():
   if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(float(value)): raise EvaluationFailure('candidate','parameter %s must be finite'%name)
  return dict(candidate)
 def _requires_profile_evidence(self):
  profiles=self.config_payload.get('verification_profiles')
  return isinstance(profiles,(list,tuple)) and any(isinstance(item,Mapping) and (item.get('id')!='default' or item.get('role')!='legacy') for item in profiles)
 def _write_bootstrap(self,state):
  if not self.config_payload: raise EvaluationFailure('configuration','resolved configuration payload is required')
  atomic_write_json(self.run_dir/'analog_opt_config.resolved.json',self.config_payload)
  write_run_manifest(self.run_dir,{'config':'analog_opt_config.resolved.json','state':state,'artifacts':{}})
 def evaluate(self,candidate,replace_work_cell=False):
  if self.state_path.exists(): raise EvaluationFailure('state','evaluate run directory already has workflow state')
  self._write_bootstrap('validated'); self._save('validated')
  self.applier.create_work_cell(self.library,self.source_cell,self.work_cell,replace_work_cell)
  self._save('work_cell_created')
  return self.evaluator.evaluate(self.run_dir,'evaluate',self._candidate(candidate))
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
  self._write_bootstrap('validated'); self._save('validated'); return self._execute(False,replace_work_cell,replace_result_cell)
 def resume(self,replace_result_cell=False): return self._execute(True,False,replace_result_cell)
 def _execute(self,resume,replace_work,replace_result):
  data=self._load()
  if data is None: raise EvaluationFailure('state','resume requires workflow state')
  state=data['state']
  if state=='validated': self.applier.create_work_cell(self.library,self.source_cell,self.work_cell,replace_work); self._save('work_cell_created'); write_run_manifest(self.run_dir,{'config':'analog_opt_config.resolved.json','state':'work_cell_created','artifacts':{}}); state='work_cell_created'
  if state in ('work_cell_created','searching'):
   if state=='searching' and self.search_config.method!='random': raise EvaluationFailure('search','non-random search cannot resume without a complete best candidate')
   self._save('searching'); write_run_manifest(self.run_dir,{'config':'analog_opt_config.resolved.json','state':'searching','artifacts':{}}); search=self.search_runner(state=='searching'); parameters=self._search_best_parameters(search)
   replay_dir=self.run_dir/'best_replay'; replay=self.replay(parameters,replay_dir,None); profile_evidence=self._validate_replay(replay,parameters,replay_dir)
   best={'objective':float(replay.objective),'metrics':dict(replay.metrics),'specs':dict(replay.specs),'parameters':parameters}
   if profile_evidence is not None: best.update(profile_evidence)
   self._save('best_replayed',parameters=parameters,best=best); data=self._load(); state='best_replayed'
  parameters=data.get('parameters'); best=data.get('best')
  if state=='best_replayed':
   points=build_pvt_points(self.pvt_config); results=[]
   for point in points:
    directory=self.run_dir/'pvt'/point.point_id
    result=self.pvt_evaluator(point,parameters,directory)
    profile_evidence=self._validate_profile_evidence(result,parameters,directory,'pvt') if self._requires_profile_evidence() else None
    row=dict(pvt_result_from_evaluation(point,result,parameters))
    if profile_evidence is not None: row['metadata'].update(profile_evidence)
    results.append(row)
   summary=summarize_pvt(points,results,expected_spec_ids=tuple(best['specs']))
   if self._requires_profile_evidence():
    full_ids={item.get('id') for item in self.config_payload.get('verification_profiles',()) if isinstance(item,Mapping) and item.get('pvt_policy','full')=='full'}
    for row in summary.points:
     selected=set(row.get('metadata',{}).get('selected_profiles',()))
     if not full_ids.issubset(selected): raise EvaluationFailure('pvt','full verification profiles are missing from a PVT point')
    if summary.overall_passed is True: self._write_profile_confirmations(best,summary,parameters)
   write_pvt_results(self.run_dir,summary); pvt=_plain(summary); self._save('pvt_validated',parameters=parameters,best=best,pvt=pvt); data=self._load(); state='pvt_validated'
  pvt=data.get('pvt')
  if state=='pvt_validated':
   payload=self._report_payload(best,pvt); write_run_manifest(self.run_dir,{'config':'analog_opt_config.resolved.json','state':'reported','artifacts':payload['artifacts']}); write_result_manifest(self.run_dir,payload); write_report(self.run_dir,payload); self._save('reported',parameters=parameters,best=best,pvt=pvt); state='reported'
  if state=='reported' and pvt.get('overall_passed') is True:
   candidate_hash=_hash(parameters); profile_hash=best.get('profile_summary_hash') if self._requires_profile_evidence() else None; intent={'candidate_hash':candidate_hash,'parameters':parameters}
   if profile_hash is not None: intent['profile_summary_hash']=profile_hash
   atomic_write_json(self.run_dir/'publication.json',intent); state_data={'parameters':parameters,'best':best,'pvt':pvt,'candidate_hash':candidate_hash}
   if profile_hash is not None: state_data['profile_summary_hash']=profile_hash
   self._save('publishing',**state_data); state='publishing'
  if state=='publishing':
   data=self._load(); candidate_hash=data['candidate_hash']
   confirmation=(self.library,self.result_cell,candidate_hash)
   if data.get('profile_summary_hash') is not None: confirmation=confirmation+(data['profile_summary_hash'],)
   if not self.applier.confirm_result_cell(*confirmation): self.applier.publish_result_cell(self.library,self.work_cell,self.result_cell,self.source_cell,replace_result)
   if self.applier.confirm_result_cell(*confirmation) is not True: raise EvaluationFailure('publication','result cell publication could not be confirmed')
   published={'parameters':parameters,'best':best,'pvt':pvt,'candidate_hash':candidate_hash}
   if data.get('profile_summary_hash') is not None: published['profile_summary_hash']=data['profile_summary_hash']
   self._save('published',**published)
  return self._load()
 def _validate_replay(self,result,parameters,directory=None):
  if not isinstance(result,EvaluationResult) or result.success is not True or not math.isfinite(float(result.objective)): raise EvaluationFailure('best_replay','fresh best replay failed')
  actual=result.metadata.get('physical_candidate') if isinstance(result.metadata,Mapping) else None
  if actual!=parameters or not result.specs or not all(isinstance(v,Mapping) and v.get('passed') is True for v in result.specs.values()): raise EvaluationFailure('best_replay','fresh best replay did not confirm passing specifications and parameters')
  if not self._requires_profile_evidence(): return None
  evidence=self._validate_profile_evidence(result,parameters,directory or self.run_dir/'best_replay','best_replay')
  evidence['profile_summary']='best_replay/'+evidence['profile_summary']
  return evidence
 def _validate_profile_evidence(self,result,parameters,directory,category):
  metadata=result.metadata
  summary_hash=metadata.get('profile_summary_hash'); summary_value=metadata.get('profile_summary')
  if not isinstance(summary_hash,str) or len(summary_hash)!=64 or not isinstance(summary_value,str): raise EvaluationFailure(category,'profile summary evidence is missing')
  root=Path(directory).resolve(strict=False); summary_path=Path(summary_value)
  if not summary_path.is_absolute(): summary_path=root/summary_path
  summary_path=summary_path.resolve(strict=False)
  try: summary_path.relative_to(root)
  except ValueError as exc: raise EvaluationFailure(category,'profile summary path escapes evaluation directory') from exc
  try: summary=json.loads(summary_path.read_text(encoding='utf-8'),parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
  except Exception as exc: raise EvaluationFailure(category,'profile summary is unreadable') from exc
  selected=summary.get('selected_profiles') if isinstance(summary,Mapping) else None; profiles=summary.get('profiles') if isinstance(summary,Mapping) else None
  try: computed_summary_hash=profile_summary_hash(summary)
  except Exception as exc: raise EvaluationFailure(category,'profile summary is not strict hashable JSON') from exc
  if computed_summary_hash!=summary_hash or summary.get('candidate_hash')!=_profile_candidate_hash(parameters): raise EvaluationFailure(category,'profile summary hash or candidate does not match')
  if not isinstance(selected,list) or not selected or not isinstance(profiles,Mapping) or any(not isinstance(profiles.get(name),Mapping) or profiles[name].get('success') is not True for name in selected): raise EvaluationFailure(category,'profile summary does not confirm every selected profile')
  relative=summary_path.relative_to(root).as_posix()
  return {'profile_summary':relative,'profile_summary_hash':summary_hash,'selected_profiles':list(selected)}
 def _write_profile_confirmations(self,best,summary,parameters):
  targets={item.get('id') for item in self.config_payload.get('verification_profiles',()) if isinstance(item,Mapping) and item.get('id') in ('stability','closed_loop_slew')}
  if not targets: return
  try: replay_summary=json.loads((self.run_dir/best['profile_summary']).read_text(encoding='utf-8'))
  except Exception as exc: raise EvaluationFailure('pvt','fresh replay profile summary is unavailable for confirmation') from exc
  pending={}
  for profile_id in targets:
   profile=replay_summary.get('profiles',{}).get(profile_id); metadata=profile.get('metadata') if isinstance(profile,Mapping) else None
   required=('testbench_signature','netlist_hash','measurement_hash')
   if not isinstance(metadata,Mapping) or any(not isinstance(metadata.get(name),str) or len(metadata[name])!=64 for name in required): raise EvaluationFailure('pvt',profile_id+' confirmation evidence is incomplete')
   pvt_hashes={}
   for row in summary.points:
    point_metadata=row.get('metadata',{})
    if profile_id not in point_metadata.get('selected_profiles',()): raise EvaluationFailure('pvt',profile_id+' is missing from a required PVT point')
    value=point_metadata.get('profile_summary_hash')
    if not isinstance(value,str) or len(value)!=64: raise EvaluationFailure('pvt',profile_id+' PVT profile summary hash is missing')
    pvt_hashes[row['point_id']]=value
   pending[profile_id]={'version':1,'profile_id':profile_id,'candidate_hash':_hash(parameters),'configuration_hash':_hash(self.config_payload),'profile_summary_hash':best['profile_summary_hash'],'testbench_signature':metadata['testbench_signature'],'netlist_hash':metadata['netlist_hash'],'measurement_hash':metadata['measurement_hash'],'pvt_profile_summary_hashes':pvt_hashes}
  for profile_id,payload in pending.items(): atomic_write_json(self.run_dir/(profile_id+'.confirmed.json'),payload)
 def _report_payload(self,best,pvt):
  artifacts={'best_replay':'best_replay','pvt_results':'pvt_results.json','report':'optimization_report.md'}
  for profile_id in ('stability','closed_loop_slew'):
   name=profile_id+'.confirmed.json'
   if (self.run_dir/name).is_file(): artifacts[profile_id+'_confirmation']=name
  metric_artifacts=best.get('metrics',{}).get('artifacts',{}) if isinstance(best.get('metrics'),Mapping) else {}
  for analysis,items in metric_artifacts.items():
   if isinstance(items,Mapping):
    for kind,value in items.items(): artifacts['dc.%s.%s'%(analysis,kind)]='best_replay/'+str(value).replace('\\','/')
  return {'best':best,'pvt':pvt,'profile_evidence_required':self._requires_profile_evidence(),'failures':[],'artifacts':artifacts}

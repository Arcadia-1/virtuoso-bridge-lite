"""Lazy live adapter assembly for analog optimization."""
from __future__ import annotations
from pathlib import Path
import json
from typing import Any,Mapping
from analog_opt.apply import VirtuosoApplier
from analog_opt.analyses import build_analysis_lines
from analog_opt.evaluator import CandidateEvaluator
from analog_opt.parameters import ParameterSpace,ParameterSpec
from analog_opt.pvt import PvtConfig
from analog_opt.search import SearchConfig,run_search
from analog_opt.specs import Spec,evaluate_specs
from analog_opt.workflow import AnalogSimulationBackend,OptimizationWorkflow

class _PublicationAdapter:
 def __init__(self,applier,run_dir): self._applier=applier; self._marker=Path(run_dir)/'publication.confirmed.json'
 def __getattr__(self,name): return getattr(self._applier,name)
 def publish_result_cell(self,*args):
  self._applier.publish_result_cell(*args)
  candidate_hash=json.loads((self._marker.parent/'publication.json').read_text(encoding='utf-8'))['candidate_hash']
  from analog_opt.evaluator import atomic_write_json
  atomic_write_json(self._marker,{'candidate_hash':candidate_hash})
 def confirm_result_cell(self,library,result_cell,candidate_hash):
  try: data=json.loads(self._marker.read_text(encoding='utf-8'))
  except (OSError,ValueError,json.JSONDecodeError): return False
  return data.get('candidate_hash')==candidate_hash
def _load_client_class():
 from virtuoso_bridge import VirtuosoClient
 return VirtuosoClient

def _parameter(raw): return ParameterSpec(name=raw['name'],target=raw['target'],lower=raw['lower'],upper=raw['upper'],dtype=raw.get('dtype','float'),scale=raw.get('scale','linear'),step=raw.get('step'),instance=raw.get('instance'),property=raw.get('property'),variable=raw.get('variable'),stimulus=raw.get('stimulus'),unit=raw.get('unit'),sync_property=raw.get('sync_property'))
def _spec(raw): return Spec(metric=raw['metric'],op=raw['op'],value=raw.get('value'),lower=raw.get('lower'),upper=raw.get('upper'),weight=raw.get('weight',1),hard=raw.get('hard',False),tolerance=raw.get('tolerance',0))
def _spec_eval(specs):
 def evaluate(metrics):
  summary=evaluate_specs(metrics,specs)
  return {'objective':summary.total,'passed':summary.passed,'results':{item.spec.metric:{'passed':item.passed,'violation':item.violation} for item in summary.results}}
 return evaluate

def _build_runtime_adapters(client,config):
 """Create concrete project adapters; imports stay inside the live call."""
 from sim_io.site_config import SiteConfig
 from sim_io.sim.run import export_netlist,run_spectre
 class NetlistAdapter:
  def __init__(self): self.variables={}; self.biases={}; self.stimuli={}; self.conditions={}; self.site=SiteConfig.from_env()
  def configure(self,design_variables,biases,stimuli,conditions): self.variables=dict(design_variables); self.biases=dict(biases); self.stimuli=dict(stimuli); self.conditions=dict(conditions)
  def export_fresh(self,library,work_cell,directory):
   directory.mkdir(parents=True,exist_ok=True)
   path=export_netlist(client,library,config.design.testbench_cell,directory,site=self.site)
   if path is None: raise RuntimeError('fresh netlist export failed')
   text=Path(path).read_text(encoding='utf-8',errors='replace')
   import re
   for name,value in self.biases.items():
    pattern=r'(?m)^(\s*%s\b.*?\bdc\s*=\s*)([^\s]+)'%re.escape(name)
    text,count=re.subn(pattern,lambda m:m.group(1)+format(float(value),'.17g'),text,count=1)
    if count!=1: raise RuntimeError('bias source not found in fresh netlist: %s'%name)
   variables=dict(self.variables); variables.update(self.stimuli)
   if 'voltage' in self.conditions: variables['PVT_VOLTAGE']=self.conditions['voltage']
   if 'temperature' in self.conditions: variables['PVT_TEMPERATURE']=self.conditions['temperature']
   lines=['', 'simulator lang=spectre']
   if variables: lines.append('parameters '+' '.join('%s=%s'%(k,format(float(v),'.17g')) for k,v in sorted(variables.items())))
   lines.extend(build_analysis_lines(config.analyses))
   deck=directory/'analog_opt.scs'; deck.write_text(text.rstrip()+'\n'+'\n'.join(lines)+'\n',encoding='utf-8')
   return deck
  def confirm(self,path,names):
   text=Path(path).read_text(encoding='utf-8',errors='replace'); found={}
   import re
   requested=dict(self.variables); requested.update(self.biases); requested.update(self.stimuli)
   for name in names:
    value=requested[name]
    if re.search(r'(?<![A-Za-z0-9_])%s\s*=\s*%s(?![A-Za-z0-9_.])'%(re.escape(name),re.escape(format(float(value),'.17g'))),text): found[name]=value
   return found
 class RunnerAdapter:
  def run(self,path,directory,analyses): return run_spectre(path,directory,site=SiteConfig.from_env(),client=client)
 class MetricsAdapter:
  def __call__(self,result):
   if not getattr(result,'ok',False): raise RuntimeError('Spectre result is not successful')
   data=getattr(result,'data',None)
   if not isinstance(data,Mapping): raise RuntimeError('Spectre result data is unavailable')
   return {str(k):float(v) for k,v in data.items() if isinstance(v,(int,float)) and not isinstance(v,bool)}
 netlist=NetlistAdapter(); return VirtuosoApplier(client),netlist,RunnerAdapter(),MetricsAdapter(),None

def create_workflow(config,run_dir):
 client=_load_client_class().from_env()
 applier,netlist,runner,metric_extractor,spec_evaluator=_build_runtime_adapters(client,config)
 applier=_PublicationAdapter(applier,run_dir)
 specs=tuple(_parameter(item) for item in config.parameters); declarations=tuple(_spec(item) for item in config.specs)
 spec_evaluator=spec_evaluator or _spec_eval(declarations)
 backend=AnalogSimulationBackend(config.design.library,config.design.work_cell,specs,config.stimuli,config.analyses,declarations,applier=applier,netlist=netlist,runner=runner,metric_extractor=metric_extractor,spec_evaluator=spec_evaluator)
 evaluator=CandidateEvaluator(backend)
 space=ParameterSpace(specs); search=SearchConfig(config.search.get('method','random'),config.search.get('evaluations',20),config.search.get('seed',0))
 pvt=PvtConfig(tuple(config.pvt.get('corners',('TT',))),tuple(config.pvt.get('voltages',(3.3,))),tuple(config.pvt.get('temperatures',(25.,))))
 root=Path(run_dir)
 def replay(candidate,directory,conditions=None): return _evaluate_conditions(backend,candidate,directory,conditions or {})
 def pvt_eval(point,candidate,directory): return _evaluate_conditions(backend,candidate,directory,{'corner':point.corner,'voltage':point.voltage,'temperature':point.temperature})
 workflow=OptimizationWorkflow(root,library=config.design.library,source_cell=config.design.cell,work_cell=config.design.work_cell,result_cell=config.design.result_cell,parameter_specs=specs,applier=applier,evaluator=evaluator,search_config=search,search_runner=lambda resume:run_search(root,space,evaluator,search,resume=resume),replay=replay,pvt_config=pvt,pvt_evaluator=pvt_eval)
 return workflow

def _evaluate_conditions(backend,candidate,directory,conditions):
 directory.mkdir(parents=True,exist_ok=True)
 raw=backend(candidate,directory,conditions)
 return EvaluationResult(directory.name,raw['objective'],raw['success'],raw['metrics'],raw.get('metadata',{}),raw.get('failure'),raw.get('specs',{}))
from analog_opt.evaluator import EvaluationResult
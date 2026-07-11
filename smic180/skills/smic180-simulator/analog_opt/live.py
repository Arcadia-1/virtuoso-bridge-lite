"""Lazy live adapters for SMIC180 analog optimization."""
from __future__ import annotations
import json,math,re
from pathlib import Path
from typing import Any,Mapping
from analog_opt.analyses import build_analysis_lines
from analog_opt.apply import VirtuosoApplier
from analog_opt.evaluator import CandidateEvaluator,EvaluationResult
from analog_opt.metrics import extract_ac_metrics,extract_mos_op_metrics,extract_noise_metrics,extract_tran_metrics,merge_metrics
from analog_opt.parameters import ParameterSpace,ParameterSpec
from analog_opt.pvt import PvtConfig
from analog_opt.search import SearchConfig,run_search
from analog_opt.specs import Spec,evaluate_specs
from analog_opt.workflow import AnalogSimulationBackend,OptimizationWorkflow

def _num(value): return format(float(value),'.17g')
def _stim_record(item):
 if isinstance(item,Mapping): return {'value':item.get('value',item.get('dc')),'source_instance':item.get('source_instance')}
 value=getattr(item,'value',None); return {'value':value if value is not None else getattr(item,'dc',None),'source_instance':getattr(item,'source_instance',None)}

class NetlistAdapter:
 def __init__(self,client,site,*,library,source_tb,work_cell,exporter,base_deck_factory,corner_patcher=None): self.client=client; self.site=site; self.library=library; self.source_tb=source_tb; self.work_cell=work_cell; self.exporter=exporter; self.base_deck_factory=base_deck_factory; self.corner_patcher=corner_patcher or (lambda d,c:d); self.variables={}; self.biases={}; self.stimuli={}; self.conditions={}
 def configure(self,design_variables,biases,stimuli,conditions): self.variables=dict(design_variables); self.biases=dict(biases); self.stimuli=dict(stimuli); self.conditions=dict(conditions)
 def _prepare_tb(self):
  tb=self.source_tb+'__analog_opt'; skill='let((src dst dut master) src=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "r") dst=dbCopyCellView(src "%s" "%s" "schematic") dut=car(setof(i dst~>instances i~>name=="DUT")) master=dbOpenCellViewByType("%s" "%s" "symbol" nil "r") unless(dut&&master error("DUT/work symbol missing")) dut~>master=master schCheck(dst) dbSave(dst) printf("ANALOG_OPT_TB_OK"))'%(self.library,self.source_tb,self.library,tb,self.library,self.work_cell)
  result=self.client.execute_skill(skill,timeout=30)
  if getattr(result,'errors',None) or 'ANALOG_OPT_TB_OK' not in (getattr(result,'output','') or ''): raise RuntimeError('dedicated work-cell testbench creation failed')
  return tb
 def _source_values(self):
  values={}
  for name,item in self.stimuli.items():
   rec=_stim_record(item); values[name]=(rec['source_instance'] or 'SRC_'+name,rec['value'])
  for name,value in self.biases.items():
   if name not in values: raise RuntimeError('bias stimulus mapping is missing: '+name)
   values[name]=(values[name][0],value)
  voltage=self.conditions.get('voltage'); voltage_name=self.conditions.get('voltage_stimulus')
  if voltage is not None:
   if voltage_name not in values: raise RuntimeError('PVT voltage_stimulus mapping is missing')
   values[voltage_name]=(values[voltage_name][0],voltage)
  return values
 def export_fresh(self,library,work_cell,directory):
  directory=Path(directory); directory.mkdir(parents=True,exist_ok=True); tb=self._prepare_tb(); raw=self.exporter(self.client,library,tb,directory,site=self.site)
  if raw is None: raise RuntimeError('fresh netlist export failed')
  text=Path(raw).read_text(encoding='utf-8',errors='replace')
  if not re.search(r'(?m)^\s*subckt\s+%s\b'%re.escape(work_cell),text) or not re.search(r'(?m)^\s*DUT\s*\([^\n]*\)\s+%s\b'%re.escape(work_cell),text): raise RuntimeError('fresh export does not contain DUT work-cell subckt')
  for _,(instance,value) in self._source_values().items():
   pattern=r'(?m)^(\s*%s\s*\([^\n]*\)\s+[vi]source\b[^\n]*?\bdc\s*=\s*)([^\s]+)'%re.escape(instance)
   text,count=re.subn(pattern,lambda m:m.group(1)+_num(value),text,count=1)
   if count!=1: raise RuntimeError('source instance not found in fresh netlist: '+instance)
  deck_cfg=self.base_deck_factory(library=library,cell=tb)
  corner=self.conditions.get('corner')
  if corner: deck_cfg=self.corner_patcher(deck_cfg,str(corner).lower())
  lines=['','simulator lang=spectre']
  for model in getattr(deck_cfg,'model_includes',[]): lines.append('include "%s"%s'%(model.path,' section='+model.section if model.section else ''))
  temp=self.conditions.get('temperature')
  if temp is not None: lines.append('simulatorOptions options temp=%s'%_num(temp))
  if self.variables: lines.append('parameters '+' '.join('%s=%s'%(k,_num(v)) for k,v in sorted(self.variables.items())))
  lines.extend(build_analysis_lines(getattr(self,'analyses',[])))
  deck=directory/'analog_opt.scs'; deck.write_text(text.rstrip()+'\n'+'\n'.join(lines)+'\n',encoding='utf-8'); return deck
 def confirm(self,path,names):
  text=Path(path).read_text(encoding='utf-8',errors='replace'); result={}; sources=self._source_values()
  for name in names:
   if name in sources:
    inst,value=sources[name]
    if re.search(r'(?m)^\s*%s\b[^\n]*\bdc\s*=\s*%s(?:\s|$)'%(re.escape(inst),re.escape(_num(value))),text): result[name]=float(value)
   elif name=='temperature':
    m=re.search(r'\btemp\s*=\s*([^\s]+)',text); result[name]=float(m.group(1)) if m else None
   elif name=='corner':
    sections=re.findall(r'\bsection\s*=\s*([A-Za-z0-9_]+)',text)
    if sections: result[name]=str(self.conditions.get('corner','')).upper() if any(section.lower()==str(self.conditions.get('corner','')).lower() for section in sections) else sections[0].upper()
   elif name=='dut_cell': result[name]=self.work_cell if re.search(r'(?m)^\s*DUT\b[^\n]*\s%s\s*$'%re.escape(self.work_cell),text) else None
   elif name in self.variables:
    m=re.search(r'\b%s\s*=\s*([^\s]+)'%re.escape(name),text); result[name]=float(m.group(1)) if m else None
  return {k:v for k,v in result.items() if v is not None}
 def confirm_cdf(self,path,specs):
  text=Path(path).read_text(encoding='utf-8',errors='replace'); block=re.search(r'(?ms)^\s*subckt\s+%s\b.*?^\s*ends\s+%s\b'%(re.escape(self.work_cell),re.escape(self.work_cell)),text)
  if not block: return {}
  result={}
  for spec in specs:
   line=re.search(r'(?m)^\s*%s\b([^\n]*)'%re.escape(spec.instance),block.group(0))
   if line:
    value=re.search(r'\b%s\s*=\s*([^\s]+)'%re.escape(spec.property),line.group(1))
    if value:
     try: result[spec.name]=float(value.group(1))
     except ValueError: pass
  return result

class MetricsAdapter:
 def __init__(self,analyses): self.analyses=tuple(analyses)
 def __call__(self,result):
  if not getattr(result,'ok',False) or not isinstance(getattr(result,'data',None),Mapping): raise RuntimeError('Spectre result unavailable')
  data=result.data; maps=[]; curves={}
  for analysis in self.analyses:
   name=analysis['name']; kind=analysis['type']; signal=analysis.get('signal',analysis.get('output','VOUT'))
   if kind=='ac':
    response=data.get('ac:'+signal); freq=data.get('freq'); maps.append(extract_ac_metrics(name,freq,response)); curves[name]={'frequency':freq,'response':[[float(v.real),float(v.imag)] if isinstance(v,complex) else float(v) for v in response] if response is not None else None}
   elif kind=='noise':
    density=data.get('noise:'+signal); freq=data.get('freq'); maps.append(extract_noise_metrics(name,freq,density)); curves[name]={'frequency':freq,'density':density}
   elif kind=='tran':
    values=data.get(signal); times=data.get('time'); maps.append(extract_tran_metrics(name,signal,times,values,target=analysis['target'],settling_tolerance=analysis.get('settling_tolerance',.02))); curves[name]={'time':times,'values':values}
   elif kind=='dc_op':
    for inst in analysis.get('instances',[]): maps.append(extract_mos_op_metrics(inst,data.get('op:'+inst,{})))
   elif kind=='dc_sweep':
    x=data.get(analysis['parameter']); y=data.get('dc:'+signal,data.get(signal)); curves[name]={'x':x,'y':y}
  output=merge_metrics(*maps); output['curves']=curves; return output

class PublicationAdapter:
 def __init__(self,applier,run_dir,specs,candidate_provider): self._applier=applier; self.run_dir=Path(run_dir); self.specs=tuple(s for s in specs if s.target=='virtuoso_cdf'); self.candidate_provider=candidate_provider
 def __getattr__(self,name): return getattr(self._applier,name)
 def publish_result_cell(self,*args): self._applier.publish_result_cell(*args)
 def confirm_result_cell(self,library,result_cell,candidate_hash):
  try:
   if hasattr(self._applier,'cell_exists'):
    exists=self._applier.cell_exists(library,result_cell)
   else:
    bridge=self._applier.client.execute_skill('if(ddGetObj(\"%s\" \"%s\") t nil)'%(library,result_cell),timeout=30)
    exists=not getattr(bridge,'errors',None) and (getattr(bridge,'output','') or '').strip().lower()=='t'
   if exists is not True: return False
   actual=self._applier.read_cdf(library,result_cell,self.specs); expected={k:v for k,v in self.candidate_provider().items() if k in {s.name for s in self.specs}}
   return set(actual)==set(expected) and all(math.isfinite(float(actual[k])) and math.isclose(float(actual[k]),float(v),rel_tol=1e-9,abs_tol=1e-15) for k,v in expected.items())
  except Exception: return False

def _load_client_class():
 from virtuoso_bridge import VirtuosoClient
 return VirtuosoClient
def _parameter(r): return ParameterSpec(name=r['name'],target=r['target'],lower=r['lower'],upper=r['upper'],dtype=r.get('dtype','float'),scale=r.get('scale','linear'),step=r.get('step'),instance=r.get('instance'),property=r.get('property'),variable=r.get('variable'),stimulus=r.get('stimulus'),unit=r.get('unit'),sync_property=r.get('sync_property'))
def _spec(r): return Spec(metric=r['metric'],op=r['op'],value=r.get('value'),lower=r.get('lower'),upper=r.get('upper'),weight=r.get('weight',1),hard=r.get('hard',False),tolerance=r.get('tolerance',0))
def _spec_eval(specs):
 def call(metrics):
  summary=evaluate_specs(metrics,specs); return {'objective':summary.total,'passed':summary.passed,'results':{x.spec.metric:{'passed':x.passed,'violation':x.violation} for x in summary.results}}
 return call

def _build_runtime_adapters(client,config,specs,run_dir):
 from sim_io.site_config import SiteConfig
 from sim_io.sim.run import export_netlist,run_spectre
 from sim_io.sim.config import resolve_sim_config
 from sim_io.sim.corner import patch_corner
 site=SiteConfig.from_env(); netlist=NetlistAdapter(client,site,library=config.design.library,source_tb=config.design.testbench_cell,work_cell=config.design.work_cell,exporter=export_netlist,base_deck_factory=lambda **k:resolve_sim_config(run_dir=run_dir,lib=k['library'],cell=k['cell']),corner_patcher=patch_corner); netlist.analyses=config.analyses
 class Runner:
  def run(self,path,directory,analyses): return run_spectre(path,directory,site=site,client=client)
 return VirtuosoApplier(client),netlist,Runner(),MetricsAdapter(config.analyses)

def create_workflow(config,run_dir):
 client=_load_client_class().from_env(); specs=tuple(_parameter(x) for x in config.parameters); declarations=tuple(_spec(x) for x in config.specs); raw,netlist,runner,metrics=_build_runtime_adapters(client,config,specs,run_dir); holder={}
 applier=PublicationAdapter(raw,run_dir,specs,lambda:holder.get('candidate',{})); backend=AnalogSimulationBackend(config.design.library,config.design.work_cell,specs,config.stimuli,config.analyses,declarations,applier=applier,netlist=netlist,runner=runner,metric_extractor=metrics,spec_evaluator=_spec_eval(declarations)); evaluator=CandidateEvaluator(backend); space=ParameterSpace(specs); search=SearchConfig(config.search.get('method','random'),config.search.get('evaluations',20),config.search.get('seed',0)); pvt=PvtConfig(tuple(config.pvt.get('corners',('TT',))),tuple(config.pvt.get('voltages',(3.3,))),tuple(config.pvt.get('temperatures',config.pvt.get('temperatures_c',(25.,))))); root=Path(run_dir)
 def evaluate(candidate,directory,conditions=None): holder['candidate']=dict(candidate); directory.mkdir(parents=True,exist_ok=True); raw_result=backend(candidate,directory,conditions or {}); return EvaluationResult(directory.name,raw_result['objective'],True,raw_result['metrics'],raw_result['metadata'],None,raw_result['specs'])
 def pvt_eval(point,candidate,directory): return evaluate(candidate,directory,{'corner':point.corner,'voltage':point.voltage,'temperature':point.temperature,'voltage_stimulus':config.pvt.get('voltage_stimulus')})
 return OptimizationWorkflow(root,library=config.design.library,source_cell=config.design.cell,work_cell=config.design.work_cell,result_cell=config.design.result_cell,parameter_specs=specs,applier=applier,evaluator=evaluator,search_config=search,search_runner=lambda resume:run_search(root,space,evaluator,search,resume=resume),replay=evaluate,pvt_config=pvt,pvt_evaluator=pvt_eval)
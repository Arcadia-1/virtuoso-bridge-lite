"""Lazy live adapters for SMIC180 analog optimization."""
from __future__ import annotations
import copy,json,math,re,uuid
from pathlib import Path
from typing import Any,Mapping
from analog_opt.analyses import AnalysisError, build_analysis_lines, required_source_parameters
from analog_opt.apply import VirtuosoApplier
from analog_opt.evaluator import CandidateEvaluator,EvaluationResult,atomic_write_json
from analog_opt.metrics import extract_ac_metrics,extract_mos_op_metrics,extract_noise_metrics,extract_tran_metrics,merge_metrics
from analog_opt.parameters import ParameterSpace,ParameterSpec
from analog_opt.pvt import PvtConfig
from analog_opt.search import SearchConfig,run_search
from analog_opt.specs import Spec,evaluate_specs
from analog_opt.workflow import AnalogSimulationBackend,OptimizationWorkflow
from analog_opt.units import parse_quantity

def _num(value): return format(float(value),'.17g')
def _stim_record(item):
 if isinstance(item,Mapping): return {'value':item.get('value',item.get('dc')),'source_instance':item.get('source_instance')}
 value=getattr(item,'value',None); return {'value':value if value is not None else getattr(item,'dc',None),'source_instance':getattr(item,'source_instance',None)}

def _logical_text(text):
 return re.sub(r'\\\s*\n\s*',' ',text)
def _spectre_number(token,dimension='length'):
 token=token.strip().strip('()')
 try: return float(token)
 except ValueError: pass
 suffix={'t':1e12,'g':1e9,'meg':1e6,'k':1e3,'m':1e-3,'u':1e-6,'n':1e-9,'p':1e-12,'f':1e-15}
 match=re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)(meg|[tgkmunpf])',token,re.I)
 if not match: raise ValueError('unsupported Spectre number: '+token)
 suffix_name=match.group(2).lower(); unit_map={'length':{'m':'mm','u':'um','n':'nm'}}
 if dimension in unit_map and suffix_name in unit_map[dimension]: return parse_quantity(match.group(1)+unit_map[dimension][suffix_name],dimension)
 return float(match.group(1))*suffix[suffix_name]
def patch_smic180_corner(deck,corner):
 patched=copy.deepcopy(deck); target=str(corner).lower()
 for model in getattr(patched,'model_includes',[]):
  section=getattr(model,'section','')
  if not section: continue
  if target in ('fnsp','snfp'):
   if section.lower()=='tt': model.section=target
  elif 'tt' in section.lower(): model.section=re.sub('tt',target,section,flags=re.I)
 return patched

class NetlistAdapter:
 def __init__(self,client,site,*,library,source_tb,work_cell,exporter,base_deck_factory,corner_patcher=None): self.client=client; self.site=site; self.library=library; self.source_tb=source_tb; self.work_cell=work_cell; self.exporter=exporter; self.base_deck_factory=base_deck_factory; self.corner_patcher=corner_patcher or (lambda d,c:d); self.analyses=[]; self.variables={}; self.biases={}; self.stimuli={}; self.conditions={}
 def configure(self,design_variables,biases,stimuli,conditions): self.variables=dict(design_variables); self.biases=dict(biases); self.stimuli=dict(stimuli); self.conditions=dict(conditions)
 def _prepare_tb(self):
  tb=self.source_tb+'__analog_opt_'+uuid.uuid4().hex[:10]
  skill=('let((src dst dut master newDut transform props cdfPairs prop pair param) unwindProtect(progn('
         'src=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "r") '
         'unless(src error("source TB missing")) dst=dbCopyCellView(src "%s" "%s" "schematic") '
         'unless(dst error("dedicated TB copy failed")) '
         'unless(length(setof(i dst~>instances i~>name=="DUT"))==1 error("DUT must be unique")) '
         'dut=car(setof(i dst~>instances i~>name=="DUT")) transform=dut~>transform props=dut~>prop cdfPairs=nil '
         'foreach(param cdfGetInstCDF(dut)~>parameters cdfPairs=cons(list(param~>name param~>value) cdfPairs)) '
         'master=dbOpenCellViewByType("%s" "%s" "symbol" nil "r") unless(master error("work symbol missing")) '
         'dbDeleteObject(dut) newDut=dbCreateInst(dst master "DUT" car(transform) cadr(transform) caddr(transform)) '
         'unless(newDut error("DUT rebuild failed")) '
         'foreach(prop props dbCreateProp(newDut prop~>name prop~>valueType prop~>value)) '
         'foreach(pair cdfPairs param=car(setof(p cdfGetInstCDF(newDut)~>parameters p~>name==car(pair))) when(param param~>value=cadr(pair))) '
         'unless(schCheck(dst) error("dedicated TB schCheck failed")) unless(dbSave(dst) error("dedicated TB save failed")) printf("ANALOG_OPT_TB_OK")) '
         'when(src dbClose(src)) when(master dbClose(master)) when(dst dbClose(dst))))')%(self.library,self.source_tb,self.library,tb,self.library,self.work_cell)
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
  circuit=_logical_text(Path(raw).read_text(encoding='utf-8',errors='replace'))
  if not re.search(r'(?mi)^\s*subckt\s+%s\b'%re.escape(work_cell),circuit) or not re.search(r'(?mi)^\s*DUT\s*\([^\n]*\)\s+%s\b'%re.escape(work_cell),circuit): raise RuntimeError('fresh export does not contain DUT work-cell subckt')
  source_values=self._source_values(); source_parameters=required_source_parameters(self.analyses); decks={}
  for analysis in self.analyses:
   text=circuit
   for stimulus,(instance,value) in source_values.items():
    replacement_value=source_parameters.get(stimulus,_num(value)) if analysis['type']=='dc_sweep' else _num(value)
    pattern=r'(?mi)^(\s*%s\s*\([^\n]*\)\s+[vi]source\b[^\n]*?\bdc\s*=\s*)(\([^\n]*?\)|[^\s]+)'%re.escape(instance)
    text,count=re.subn(pattern,lambda m:m.group(1)+replacement_value,text,count=1)
    if count!=1: raise RuntimeError('source instance not found in fresh netlist: '+instance)
   deck_cfg=self.base_deck_factory(library=library,cell=tb); corner=self.conditions.get('corner')
   if corner: deck_cfg=self.corner_patcher(deck_cfg,str(corner).lower())
   lines=['','simulator lang=spectre']
   for model in getattr(deck_cfg,'model_includes',[]): lines.append('include "%s"%s'%(model.path,' section='+model.section if model.section else ''))
   temp=self.conditions.get('temperature')
   if temp is not None: lines.append('simulatorOptions options temp=%s'%_num(temp))
   if self.variables: lines.append('parameters '+' '.join('%s=%s'%(k,_num(v)) for k,v in sorted(self.variables.items())))
   analysis_lines=build_analysis_lines([analysis])
   if analysis.get('type')=='dc_op' and analysis.get('instances'):
    analysis_lines=[*(f"save {instance}:oppoint" for instance in analysis['instances']),*analysis_lines,'opInfo info what=oppoint where=rawfile']
   lines.extend(analysis_lines); target=directory/analysis['name']; target.mkdir(parents=True,exist_ok=True); deck=target/'analog_opt.scs'; deck.write_text(text.rstrip()+'\n'+'\n'.join(lines)+'\n',encoding='utf-8'); decks[analysis['name']]=deck
  return decks

 def confirm(self,decks,expected_by_analysis):
  if not isinstance(decks,Mapping) or not isinstance(expected_by_analysis,Mapping): raise ValueError('analysis-specific confirmation requires mappings')
  output={}; sources=self._source_values()
  for analysis_name,expected in expected_by_analysis.items():
   if analysis_name not in decks: continue
   text=Path(decks[analysis_name]).read_text(encoding='utf-8',errors='replace'); result={}
   for name,want in expected.items():
    if name in sources:
     inst,_=sources[name]; match=re.search(r'(?mi)^\s*%s\b[^\n]*\bdc\s*=\s*([^\s]+)'%re.escape(inst),text)
     if match:
      try: result[name]=_spectre_number(match.group(1),'scalar')
      except ValueError: pass
    elif isinstance(want,str) and name not in ('dut_cell','corner'):
     for stimulus,(inst,_) in sources.items():
      match=re.search(r'(?mi)^\s*%s\b[^\n]*\bdc\s*=\s*%s(?:\s|$)'%(re.escape(inst),re.escape(want)),text)
      if match: result[name]=want; break
    elif name=='temperature':
     match=re.search(r'\btemp\s*=\s*([^\s]+)',text); result[name]=float(match.group(1)) if match else None
    elif name=='corner':
     sections=re.findall(r'\bsection\s*=\s*([A-Za-z0-9_]+)',text); result[name]=str(want).upper() if any(x.lower()==str(want).lower() for x in sections) else None
    elif name=='dut_cell': result[name]=self.work_cell if re.search(r'(?m)^\s*DUT\b[^\n]*\s%s\s*$'%re.escape(self.work_cell),text) else None
    elif name in self.variables:
     match=re.search(r'\b%s\s*=\s*([^\s]+)'%re.escape(name),text); result[name]=float(match.group(1)) if match else None
   output[analysis_name]={k:v for k,v in result.items() if v is not None}
  return output

 def confirm_cdf(self,path,specs):
  if isinstance(path,Mapping): path=next(iter(path.values()))
  text=_logical_text(Path(path).read_text(encoding='utf-8',errors='replace')); block=re.search(r'(?mis)^\s*subckt\s+%s\b.*?^\s*ends\s+%s\b'%(re.escape(self.work_cell),re.escape(self.work_cell)),text)
  if not block: raise ValueError('complete work-cell subckt unavailable')
  result={}
  for spec in specs:
   line=re.search(r'(?mi)^\s*%s\b([^\n]*)'%re.escape(spec.instance),block.group(0))
   if line:
    value=re.search(r'\b%s\s*=\s*(\([^\n]*?\)|[^\s]+)'%re.escape(spec.property),line.group(1),re.I)
    if value: result[spec.name]=_spectre_number(value.group(1),'length' if spec.unit in ('m','mm','um','nm') else 'scalar')
  if set(result)!={spec.name for spec in specs}: raise ValueError('complete CDF parameter set unavailable in DUT subckt')
  return result


class AnalysisRunner:
 def __init__(self,run_one): self.run_one=run_one
 def run(self,decks,directory,analyses):
  results={}
  for analysis in analyses:
   name=analysis['name']; target=Path(directory)/name; target.mkdir(parents=True,exist_ok=True)
   results[name]=self.run_one(decks[name],target)
  return results

class MetricsAdapter:
 def __init__(self,analyses): self.analyses=tuple(analyses)
 def __call__(self,results):
  if not isinstance(results,Mapping): raise RuntimeError('analysis results must be a mapping')
  maps=[]; curves={}
  for analysis in self.analyses:
   name=analysis['name']; result=results.get(name)
   if result is None or not getattr(result,'ok',False) or not isinstance(getattr(result,'data',None),Mapping): raise RuntimeError('Spectre result unavailable for '+name)
   data=result.data; kind=analysis['type']; signal=analysis.get('signal',analysis.get('output','VOUT'))
   if kind=='ac':
    response=data.get('ac:'+signal); freq=data.get('freq'); maps.append(extract_ac_metrics(name,freq,response)); curves[name]={'frequency':freq,'response':[[float(v.real),float(v.imag)] if isinstance(v,complex) else float(v) for v in response] if response is not None else None}
   elif kind=='noise':
    density=data.get('noise:'+signal); freq=data.get('noise_freq',data.get('freq')); maps.append(extract_noise_metrics(name,freq,density)); curves[name]={'frequency':freq,'density':density}
   elif kind=='tran':
    values=data.get(signal); times=data.get('time'); maps.append(extract_tran_metrics(name,signal,times,values,target=analysis['target'],settling_tolerance=analysis.get('settling_tolerance',.02))); curves[name]={'time':times,'values':values}
   elif kind=='dc_op':
    for inst in analysis.get('instances',[]):
     op=data.get('op:'+inst)
     if not isinstance(op,Mapping): raise AnalysisError('operating-point data unavailable for '+inst)
     maps.append(extract_mos_op_metrics(inst,op))
   elif kind=='dc_sweep':
    x=data.get(analysis['parameter']); y=data.get('dc:'+signal,data.get(signal)); curves[name]={'x':x,'y':y}
  output=merge_metrics(*maps); output['curves']=curves; return output

class PublicationAdapter:
 def __init__(self,applier,run_dir,specs,candidate_provider): self._applier=applier; self.run_dir=Path(run_dir); self.specs=tuple(s for s in specs if s.target=='virtuoso_cdf'); self.candidate_provider=candidate_provider
 def __getattr__(self,name): return getattr(self._applier,name)
 def publish_result_cell(self,*args):
  self._applier.publish_result_cell(*args)
  intent=json.loads((self.run_dir/'publication.json').read_text(encoding='utf-8')); atomic_write_json(self.run_dir/'publication.confirmed.json',{'candidate_hash':intent['candidate_hash']})
 def confirm_result_cell(self,library,result_cell,candidate_hash):
  try:
   if hasattr(self._applier,'cell_exists'):
    exists=self._applier.cell_exists(library,result_cell)
   else:
    bridge=self._applier.client.execute_skill('if(ddGetObj(\"%s\" \"%s\") t nil)'%(library,result_cell),timeout=30)
    exists=not getattr(bridge,'errors',None) and (getattr(bridge,'output','') or '').strip().lower()=='t'
   if exists is not True: return False
   candidate=self.candidate_provider()
   if not self.specs:
    intent=json.loads((self.run_dir/'publication.json').read_text(encoding='utf-8')); marker=json.loads((self.run_dir/'publication.confirmed.json').read_text(encoding='utf-8'))
    return intent.get('candidate_hash')==candidate_hash and marker.get('candidate_hash')==candidate_hash and intent.get('parameters')==candidate
   actual=self._applier.read_cdf(library,result_cell,self.specs); expected={k:v for k,v in candidate.items() if k in {s.name for s in self.specs}}
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
 site=SiteConfig.from_env(); netlist=NetlistAdapter(client,site,library=config.design.library,source_tb=config.design.testbench_cell,work_cell=config.design.work_cell,exporter=export_netlist,base_deck_factory=lambda **k:resolve_sim_config(run_dir=run_dir,lib=k['library'],cell=k['cell']),corner_patcher=patch_smic180_corner); netlist.analyses=config.analyses
 runner=AnalysisRunner(lambda path,directory:run_spectre(path,directory,site=site,client=client))
 return VirtuosoApplier(client),netlist,runner,MetricsAdapter(config.analyses)

def create_workflow(config,run_dir):
 client=_load_client_class().from_env(); specs=tuple(_parameter(x) for x in config.parameters); declarations=tuple(_spec(x) for x in config.specs); raw,netlist,runner,metrics=_build_runtime_adapters(client,config,specs,run_dir); holder={}
 applier=PublicationAdapter(raw,run_dir,specs,lambda:holder.get('candidate',{})); backend=AnalogSimulationBackend(config.design.library,config.design.work_cell,specs,config.stimuli,config.analyses,declarations,applier=applier,netlist=netlist,runner=runner,metric_extractor=metrics,spec_evaluator=_spec_eval(declarations)); evaluator=CandidateEvaluator(backend); space=ParameterSpace(specs); search=SearchConfig(config.search.get('method','random'),config.search.get('evaluations',20),config.search.get('seed',0)); pvt=PvtConfig(tuple(config.pvt.get('corners',('TT',))),tuple(config.pvt.get('voltages',(3.3,))),tuple(config.pvt.get('temperatures',config.pvt.get('temperatures_c',(25.,))))); root=Path(run_dir)
 def evaluate(candidate,directory,conditions=None): holder['candidate']=dict(candidate); directory.mkdir(parents=True,exist_ok=True); raw_result=backend(candidate,directory,conditions or {}); return EvaluationResult(directory.name,raw_result['objective'],True,raw_result['metrics'],raw_result['metadata'],None,raw_result['specs'])
 def pvt_eval(point,candidate,directory): return evaluate(candidate,directory,{'corner':point.corner,'voltage':point.voltage,'temperature':point.temperature,'voltage_stimulus':config.pvt.get('voltage_stimulus')})
 return OptimizationWorkflow(root,library=config.design.library,source_cell=config.design.cell,work_cell=config.design.work_cell,result_cell=config.design.result_cell,parameter_specs=specs,applier=applier,evaluator=evaluator,search_config=search,search_runner=lambda resume:run_search(root,space,evaluator,search,resume=resume),replay=evaluate,pvt_config=pvt,pvt_evaluator=pvt_eval)
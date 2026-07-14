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
from analog_opt.profile_testbenches import confirm_profile_netlist
from analog_opt.profiles import MultiProfileBackend,ProfileRuntime
from analog_opt.pvt import PvtConfig
from analog_opt.search import SearchConfig,run_search
from analog_opt.slew import extract_closed_loop_slew
from analog_opt.specs import Spec,evaluate_specs
from analog_opt.stability import extract_stability_metrics
from analog_opt.schema import canonical_resolved_payload
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
 if dimension=='length' and re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?(?:mm|um|nm)',token,re.I): return parse_quantity(token.lower(),dimension)
 try: return float(token)
 except ValueError: pass
 suffix={'t':1e12,'g':1e9,'meg':1e6,'k':1e3,'m':1e-3,'u':1e-6,'n':1e-9,'p':1e-12,'f':1e-15}
 match=re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)(meg|[tgkmunpf])',token,re.I)
 if not match: raise ValueError('unsupported Spectre number: '+token)
 suffix_name=match.group(2).lower(); unit_map={'length':{'m':'mm','u':'um','n':'nm'}}
 if dimension in unit_map and suffix_name in unit_map[dimension]: return parse_quantity(match.group(1)+unit_map[dimension][suffix_name],dimension)
 return float(match.group(1))*suffix[suffix_name]
def patch_smic180_corner(deck,corner,core_model_include=None):
 patched=copy.deepcopy(deck); target=str(corner).lower()
 core_identity=str(core_model_include).replace('\\','/').lower() if core_model_include else None
 for model in getattr(patched,'model_includes',[]):
  section=getattr(model,'section','')
  if not section: continue
  if re.fullmatch(r'mim_(?:tt|ff|ss)',section,re.I):
   model.section={'ff':'mim_ff','ss':'mim_ss'}.get(target,'mim_tt')
   continue
  model_identity=str(getattr(model,'path','')).replace('\\','/').lower()
  explicit_core=core_identity is not None and model_identity==core_identity
  legacy_core=core_identity is None and re.search(r'core|mos|nch|pch',model_identity+' '+section,re.I)
  if re.search(r'(^|_)tt($|_)',section,re.I) and (explicit_core or legacy_core):
   model.section=re.sub(r'(^|_)tt(?=$|_)',lambda m:m.group(1)+target,section,flags=re.I)
 return patched
class NetlistAdapter:
 def __init__(self,client,site,*,library,source_tb,work_cell,dut_instance="DUT",exporter,base_deck_factory,corner_patcher=None): self.client=client; self.site=site; self.library=library; self.source_tb=source_tb; self.work_cell=work_cell; self.dut_instance=dut_instance; self.exporter=exporter; self.base_deck_factory=base_deck_factory; self.corner_patcher=corner_patcher or (lambda d,c:d); self.analyses=[]; self.variables={}; self.biases={}; self.stimuli={}; self.conditions={}
 def configure(self,design_variables,biases,stimuli,conditions): self.variables=dict(design_variables); self.biases=dict(biases); self.stimuli=dict(stimuli); self.conditions=dict(conditions)
 def confirm_profile(self,profile,deck,expectation):
  path=Path(deck)
  if not path.is_file(): raise RuntimeError('profile netlist is missing: '+str(path))
  return confirm_profile_netlist(profile,path.read_text(encoding='utf-8'),expectation)
 def _tb_step(self,skill,sentinel):
  result=self.client.execute_skill("progn(\n"+skill+"\n)",timeout=30)
  errors=getattr(result,'errors',None) or ()
  raw=(getattr(result,'output','') or '').strip()
  try: output=json.loads(raw) if raw.startswith('\"') and raw.endswith('\"') else raw
  except json.JSONDecodeError: output=raw.strip('\"')
  if errors or not output.startswith(sentinel): raise RuntimeError('%s failed: errors=%r output=%r'%(sentinel,errors,getattr(result,'output','') or ''))
  return output
 def _tb_literal(self,value,label):
  value=value.strip()
  if not value or len(value)>20000 or '\n' in value or '\r' in value or ';' in value: raise RuntimeError('invalid '+label+' snapshot')
  depth=0; quoted=False; escaped=False
  for char in value:
   if quoted:
    if escaped: escaped=False
    elif char=='\\': escaped=True
    elif char=='"': quoted=False
   elif char=='"': quoted=True
   elif char=='(': depth+=1
   elif char==')':
    depth-=1
    if depth<0: raise RuntimeError('invalid '+label+' snapshot')
  if quoted or depth!=0: raise RuntimeError('invalid '+label+' snapshot')
  return value
 def _prepare_tb(self):
  tb=self.source_tb+'__analog_opt_'+uuid.uuid4().hex[:10]; copied=False
  try:
   copy=('let((src dst) when(ddGetObj("%s" "%s") error("dedicated TB already exists")) '
         'src=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "r") unless(src error("source TB missing")) '
         'dst=dbCopyCellView(src "%s" "%s" "schematic") unless(dst error("dedicated TB copy failed")) '
         'unless(dbSave(dst) error("dedicated TB copy save failed")) when(src dbClose(src)) when(dst dbClose(dst)) "ANALOG_OPT_TB_COPY_OK")')%(self.library,tb,self.library,self.source_tb,self.library,tb)
   self._tb_step(copy,'ANALOG_OPT_TB_COPY_OK'); copied=True
   snapshot=('let((cv dut props cdfPairs) cv=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "r") '
             'unless(cv error("dedicated TB open failed")) unless(length(setof(i cv~>instances i~>name=="%s"))==1 error("DUT must be unique")) '
             'dut=car(setof(i cv~>instances i~>name=="%s")) props=mapcar(lambda((p) list(p~>name p~>valueType p~>value)) dut~>prop) '
             'cdfPairs=mapcar(lambda((p) list(p~>name p~>value)) cdfGetInstCDF(dut)~>parameters) '
             'prog1(sprintf(nil "ANALOG_OPT_TB_SNAPSHOT|%%L|%%L|%%L" dut~>transform props cdfPairs) dbClose(cv)))')%(self.library,tb,self.dut_instance,self.dut_instance)
   raw=self._tb_step(snapshot,'ANALOG_OPT_TB_SNAPSHOT|'); parts=raw.split('|',3)
   if len(parts)!=4: raise RuntimeError('invalid dedicated TB snapshot')
   transform=self._tb_literal(parts[1],'transform'); props=self._tb_literal(parts[2],'property'); cdf=self._tb_literal(parts[3],'CDF')
   delete=('let((cv dut) cv=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "a") unless(cv error("dedicated TB open failed")) '
           'unless(length(setof(i cv~>instances i~>name=="%s"))==1 error("DUT must be unique")) dut=car(setof(i cv~>instances i~>name=="%s")) '
           'dbDeleteObject(dut) unless(dbSave(cv) error("DUT delete save failed")) when(cv dbClose(cv)) "ANALOG_OPT_TB_DELETE_DUT_OK")')%(self.library,tb,self.dut_instance,self.dut_instance)
   self._tb_step(delete,'ANALOG_OPT_TB_DELETE_DUT_OK')
   create=('let((cv master transform newDut) cv=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "a") unless(cv error("dedicated TB open failed")) '
           'master=dbOpenCellViewByType("%s" "%s" "symbol" nil "r") unless(master error("work symbol missing")) transform=quote(%s) '
           'newDut=dbCreateInst(cv master "%s" car(transform) cadr(transform)) unless(newDut error("DUT rebuild failed")) '
           'unless(dbSave(cv) error("DUT create save failed")) when(master dbClose(master)) when(cv dbClose(cv)) "ANALOG_OPT_TB_CREATE_DUT_OK")')%(self.library,tb,self.library,self.work_cell,transform,self.dut_instance)
   self._tb_step(create,'ANALOG_OPT_TB_CREATE_DUT_OK')
   restore_props=('let((cv dut props) cv=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "a") unless(cv error("dedicated TB open failed")) '
                  'dut=car(setof(i cv~>instances i~>name=="%s")) unless(dut error("rebuilt DUT missing")) props=quote(%s) '
                  'foreach(pair props dbCreateProp(dut car(pair) cadr(pair) caddr(pair))) unless(dbSave(cv) error("property restore save failed")) '
                  'when(cv dbClose(cv)) "ANALOG_OPT_TB_RESTORE_PROPS_OK")')%(self.library,tb,self.dut_instance,props)
   self._tb_step(restore_props,'ANALOG_OPT_TB_RESTORE_PROPS_OK')
   restore_cdf=('let((cv dut pairs param) cv=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "a") unless(cv error("dedicated TB open failed")) '
                'dut=car(setof(i cv~>instances i~>name=="%s")) unless(dut error("rebuilt DUT missing")) pairs=quote(%s) '
                'foreach(pair pairs param=car(setof(p cdfGetInstCDF(dut)~>parameters p~>name==car(pair))) when(param param~>value=cadr(pair))) '
                'unless(dbSave(cv) error("CDF restore save failed")) when(cv dbClose(cv)) "ANALOG_OPT_TB_RESTORE_CDF_OK")')%(self.library,tb,self.dut_instance,cdf)
   self._tb_step(restore_cdf,'ANALOG_OPT_TB_RESTORE_CDF_OK')
   final=('let((cv) cv=dbOpenCellViewByType("%s" "%s" "schematic" "schematic" "a") unless(cv error("dedicated TB open failed")) '
          'unless(length(setof(i cv~>instances i~>name=="%s"))==1 error("rebuilt DUT must be unique")) unless(schCheck(cv) error("dedicated TB schCheck failed")) '
          'unless(dbSave(cv) error("dedicated TB save failed")) when(cv dbClose(cv)) "ANALOG_OPT_TB_OK")')%(self.library,tb,self.dut_instance)
   self._tb_step(final,'ANALOG_OPT_TB_OK'); return tb
  except Exception:
   if copied:
    try: self._delete_tb(tb)
    except Exception as cleanup_error:
     raise RuntimeError("dedicated TB cleanup failed; cell retained: %s (%s)"%(tb,cleanup_error)) from cleanup_error
   raise
 def _delete_tb(self,tb):
  if not tb.startswith(self.source_tb+'__analog_opt_'): raise RuntimeError('refusing to delete non-dedicated testbench')
  skill=('let((obj ok) obj=ddGetObj("%s" "%s") unless(obj error("dedicated TB missing")) ok=ddDeleteObj(obj) '
         'unless(ok error("dedicated TB cleanup failed")) when(ddGetObj("%s" "%s") error("dedicated TB still exists")) "ANALOG_OPT_TB_DELETE_OK")')%(self.library,tb,self.library,tb)
  self._tb_step(skill,'ANALOG_OPT_TB_DELETE_OK')
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
  directory=Path(directory); directory.mkdir(parents=True,exist_ok=True); tb=self._prepare_tb()
  try:
   raw=self.exporter(self.client,library,tb,directory,site=self.site)
   if raw is None: raise RuntimeError('fresh netlist export failed')
   circuit=_logical_text(Path(raw).read_text(encoding='utf-8',errors='replace'))
  finally:
   self._delete_tb(tb)
  if not re.search(r'(?mi)^\s*%s\s*\([^\n]*\)\s+%s\b'%(re.escape(self.dut_instance),re.escape(work_cell)),circuit) or not re.search(r'(?mi)^\s*%s\s*\([^\n]*\)\s+%s\b'%(re.escape(self.dut_instance),re.escape(work_cell)),circuit): raise RuntimeError('fresh export does not contain DUT work-cell subckt')
  source_values=self._source_values(); decks={}
  for analysis in self.analyses:
   text=circuit
   if analysis.get('type')=='dc_sweep' and analysis.get('parameter'):
    text='parameters %s=%s\n'%(analysis['parameter'],_num(parse_quantity(analysis['start'],'voltage') if isinstance(analysis['start'],str) else analysis['start']))+text
   for stimulus,(instance,value) in source_values.items():
    replacement_value=analysis['parameter'] if analysis['type']=='dc_sweep' and analysis.get('source')==stimulus else _num(value)
    pattern=r'(?mi)^(\s*%s\s*\([^\n]*\)\s+[vi]source\b)([^\n]*)$'%re.escape(instance)
    def patch_source(match):
     tail=re.sub(r'\btype\s*=\s*[^\s]+','type=dc',match.group(2),count=1,flags=re.I)
     if re.search(r'\bdc\s*=',tail,re.I):
      tail=re.sub(r'(\bdc\s*=\s*)(\([^\n]*?\)|[^\s]+)',lambda item:item.group(1)+replacement_value,tail,count=1,flags=re.I)
     else: tail=tail.rstrip()+' dc='+replacement_value
     return match.group(1)+tail
    text,count=re.subn(pattern,patch_source,text,count=1)
    if count!=1: raise RuntimeError('source instance not found in fresh netlist: '+instance)
   deck_cfg=self.base_deck_factory(library=library,cell=tb); corner=self.conditions.get('corner')
   if corner: deck_cfg=self.corner_patcher(deck_cfg,str(corner).lower())
   lines=['','simulator lang=spectre']
   core_path=str(getattr(self.site,'pdk_core_spectre_include','') or '').replace('\\','/').lower()
   seen_includes=set()
   configured_paths=[]
   for model in getattr(deck_cfg,'model_includes',[]):
    path=str(model.path); norm=path.replace('\\','/').lower()
    if norm not in configured_paths: configured_paths.append(norm)
   for norm in configured_paths:
    pattern=r'(?mi)^\s*include\s+["\']%s["\'][^\n]*\n?'%re.escape(norm)
    text=re.sub(pattern,'',text)
   for model in getattr(deck_cfg,'model_includes',[]):
    path=str(model.path); norm=path.replace('\\','/').lower(); section=str(getattr(model,'section','') or '')
    key=(norm,section.lower())
    if key in seen_includes: continue
    seen_includes.add(key)
    lines.append('include "%s"%s'%(path,' section='+section if section else ''))
   temp=self.conditions.get('temperature')
   if temp is not None: lines.append('simulatorOptions options temp=%s'%_num(temp))
   if self.variables: lines.append('parameters '+' '.join('%s=%s'%(k,_num(v)) for k,v in sorted(self.variables.items())))
   analysis_lines=build_analysis_lines([analysis])
   if analysis.get('type')=='dc_op' and analysis.get('instances'):
    analysis_lines=[*(f"save {self.dut_instance}.{instance}:oppoint" for instance in analysis['instances']),*analysis_lines,'opInfo info what=oppoint where=rawfile']
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
    elif name=='dut_cell': result[name]=self.work_cell if re.search(r'(?m)^\s*%s\b[^\n]*\s%s\s*$'%(re.escape(self.dut_instance),re.escape(self.work_cell)),text) else None
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
   observed=[]
   for target_instance in (spec.instance,)+tuple(spec.linked_instances):
    line=re.search(r'(?mi)^\s*%s\b([^\n]*)'%re.escape(target_instance),block.group(0))
    if line:
     deck_property='w' if spec.property=='fw' else spec.property
     value=re.search(r'\b%s\s*=\s*(\([^\n]*?\)|[^\s]+)'%re.escape(deck_property),line.group(1),re.I)
     if value:
      parsed=_spectre_number(value.group(1),'length' if spec.unit in ('m','mm','um','nm') else 'scalar')
      if spec.property=='w' and spec.sync_property=='fw' and spec.sync_factor is not None: parsed/=float(spec.sync_factor)
      observed.append(parsed)
   if len(observed)==1+len(spec.linked_instances) and all(math.isclose(float(observed[0]),float(item),rel_tol=1e-9,abs_tol=1e-15) for item in observed[1:]): result[spec.name]=observed[0]
  if set(result)!={spec.name for spec in specs}: raise ValueError('complete CDF parameter set unavailable in DUT subckt')
  return result


class AnalysisRunner:
 def __init__(self,run_one): self.run_one=run_one
 def run(self,decks,directory,analyses):
  results={}
  for analysis in analyses:
   name=analysis['name']; target=Path(directory)/name; target.mkdir(parents=True,exist_ok=True)
   results[name]=self.run_one(decks[name],target)
   metadata=getattr(results[name],'metadata',None)
   if isinstance(metadata,dict): metadata['run_dir']=str(Path(directory))
  return results

class MetricsAdapter:
 def __init__(self,analyses): self.analyses=tuple(analyses)
 def load_complex_analysis(self,result,analysis):
  data=getattr(result,'data',None); metadata=getattr(result,'metadata',None)
  if not isinstance(data,Mapping) or not isinstance(metadata,Mapping): raise RuntimeError('STB result unavailable')
  result_candidates=analysis.get('result_candidates'); frequency_candidates=analysis.get('frequency_candidates')
  if not isinstance(result_candidates,(list,tuple)) or not result_candidates or not all(isinstance(x,str) and x for x in result_candidates): raise RuntimeError('STB result candidates unavailable')
  if not isinstance(frequency_candidates,(list,tuple)) or not frequency_candidates or not all(isinstance(x,str) and x for x in frequency_candidates): raise RuntimeError('STB frequency candidates unavailable')
  result_keys=[key for key in result_candidates if key in data]
  if not result_keys: raise RuntimeError('STB complex result unavailable')
  if len(result_keys)!=1: raise RuntimeError('STB requires exactly one complex result')
  frequency_keys=[key for key in frequency_candidates if key in data]
  if not frequency_keys: raise RuntimeError('STB frequency result unavailable')
  if len(frequency_keys)!=1: raise RuntimeError('STB requires exactly one frequency result')
  result_key=result_keys[0]; frequency_key=frequency_keys[0]
  started=metadata.get('analysis_started_at'); mtimes=metadata.get('result_mtimes')
  if isinstance(started,bool) or not isinstance(started,(int,float)) or not math.isfinite(float(started)) or not isinstance(mtimes,Mapping): raise RuntimeError('STB freshness metadata unavailable')
  for key in (frequency_key,result_key):
   mtime=mtimes.get(key)
   if isinstance(mtime,bool) or not isinstance(mtime,(int,float)) or not math.isfinite(float(mtime)) or float(mtime)<float(started): raise RuntimeError('STB result is stale: '+key)
  frequencies=data[frequency_key]; response=data[result_key]
  if not isinstance(frequencies,(list,tuple)) or not isinstance(response,(list,tuple)) or len(frequencies)<2 or len(frequencies)!=len(response): raise RuntimeError('STB complex curve is invalid')
  if any(isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(float(value)) for value in frequencies): raise RuntimeError('STB frequency curve is invalid')
  if any(not isinstance(value,complex) or not math.isfinite(value.real) or not math.isfinite(value.imag) for value in response): raise RuntimeError('STB response must be finite complex data')
  return list(frequencies),list(response),result_key
 def __call__(self,results):
  if not isinstance(results,Mapping): raise RuntimeError('analysis results must be a mapping')
  maps=[]; curves={}
  for analysis in self.analyses:
   name=analysis['name']; result=results.get(name)
   if result is None or not getattr(result,'ok',False) or not isinstance(getattr(result,'data',None),Mapping): raise RuntimeError('Spectre result unavailable for '+name)
   data=result.data; kind=analysis['type']; signal=analysis.get('signal',analysis.get('output','VOUT'))
   if kind=='stb':
    freq,response,result_key=self.load_complex_analysis(result,analysis)
    curves[name]={'frequency':freq,'response':[[float(value.real),float(value.imag)] for value in response],'result_key':result_key}
    if analysis.get('metric_mode')=='stability':
     maps.append(extract_stability_metrics(
      analysis.get('profile_id','default'),name,freq,response,
      crossing_policy=analysis.get('crossing_policy','single'),
      require_gain_margin=analysis.get('require_gain_margin',True),
     ))
   elif kind=='ac':
    response=data.get('ac:'+signal); freq=data.get('freq'); maps.append(extract_ac_metrics(name,freq,response)); curves[name]={'frequency':freq,'response':[[float(v.real),float(v.imag)] if isinstance(v,complex) else float(v) for v in response] if response is not None else None}
   elif kind=='noise':
    density=data.get('noise:'+signal); freq=data.get('noise_freq',data.get('freq')); maps.append(extract_noise_metrics(name,freq,density)); curves[name]={'frequency':freq,'density':density}
   elif kind=='tran':
    values=data.get(signal); times=data.get('time')
    if analysis.get('metric_mode')=='closed_loop_slew':
     measurement=extract_closed_loop_slew(
      analysis.get('profile_id','default'),name,signal,times,values,
      low=analysis['low'],high=analysis['high'],
      fractions=tuple(analysis.get('fractions',(.2,.8))),
      settling_tolerance=analysis.get('settling_tolerance',.02),
      max_nonmonotonic_fraction=analysis.get('max_nonmonotonic_fraction',.1),
      min_fit_samples=analysis.get('min_fit_samples',3),
      rise_reference_time=analysis.get('rise_reference_time'),
      fall_reference_time=analysis.get('fall_reference_time'),
     )
     maps.append(measurement.metrics); curves[name]={'time':times,'values':values,'slew_evidence':measurement.evidence}
    else:
     maps.append(extract_tran_metrics(name,signal,times,values,target=analysis['target'],settling_tolerance=analysis.get('settling_tolerance',.02))); curves[name]={'time':times,'values':values}
   elif kind=='dc_op':
    for inst in analysis.get('instances',[]):
     op=data.get('op:'+inst)
     if not isinstance(op,Mapping): raise AnalysisError('operating-point data unavailable for '+inst)
     maps.append(extract_mos_op_metrics(inst,op))
    dc_metrics={}
    for node in analysis.get('nodes',[]):
     value=data.get(node,data.get('op_'+node))
     if isinstance(value,(list,tuple)): value=value[-1] if value else None
     if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(float(value)): raise AnalysisError('DC node data unavailable for '+node)
     dc_metrics['op.node.'+node]=float(value)
    for metric,signal in analysis.get('source_currents',{}).items():
     value=data.get(signal,data.get('op_'+signal))
     if isinstance(value,(list,tuple)): value=value[-1] if value else None
     if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(float(value)): raise AnalysisError('DC source current unavailable for '+signal)
     dc_metrics['op.current.'+metric]=float(value)
    maps.append(dc_metrics)
   elif kind=='dc_sweep':
    x=data.get(analysis['parameter']); y=data.get('dc:'+signal,data.get(signal))
    if not isinstance(x,(list,tuple)) or not isinstance(y,(list,tuple)) or len(x)<2 or len(x)!=len(y) or len(x)!=analysis.get('points',len(x)) or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(float(v)) for v in list(x)+list(y)): raise AnalysisError('invalid DC sweep curve for '+name)
    curves[name]={'x':list(x),'y':list(y)}
    run_root=getattr(result,'metadata',{}).get('run_dir')
    if run_root:
     target=Path(run_root)/name; target.mkdir(parents=True,exist_ok=True); svg=target/('dc_'+name+'.svg')
     xmin,xmax=min(x),max(x); ymin,ymax=min(y),max(y); dx=xmax-xmin or 1.; dy=ymax-ymin or 1.
     points=['%.3f,%.3f'%(20+360*(float(xv)-xmin)/dx,180-150*(float(yv)-ymin)/dy) for xv,yv in zip(x,y)]
     svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="420" height="210" viewBox="0 0 420 210"><path d="M '+(' L '.join(points))+'" fill="none" stroke="black"/></svg>\n',encoding='utf-8')
     curves.setdefault('artifacts',{})[name]={'svg':str(Path(name)/svg.name)}
  output=merge_metrics(*maps); output['curves']={k:v for k,v in curves.items() if k!='artifacts'}; output['artifacts']=curves.get('artifacts',{}); return output

class PublicationAdapter:
 def __init__(self,applier,run_dir,specs,candidate_provider): self._applier=applier; self.run_dir=Path(run_dir); self.specs=tuple(s for s in specs if s.target=='virtuoso_cdf'); self.candidate_provider=candidate_provider
 def __getattr__(self,name): return getattr(self._applier,name)
 def publish_result_cell(self,*args):
  self._applier.publish_result_cell(*args)
  intent=json.loads((self.run_dir/'publication.json').read_text(encoding='utf-8')); atomic_write_json(self.run_dir/'publication.confirmed.json',{'candidate_hash':intent['candidate_hash']})
 def confirm_result_cell(self,library,result_cell,candidate_hash):
  try:
   if hasattr(self._applier,'client'):
    bridge=self._applier.client.execute_skill('let((schematic symbol) schematic=dbOpenCellViewByType(\"%s\" \"%s\" \"schematic\" \"schematic\" \"r\") symbol=dbOpenCellViewByType(\"%s\" \"%s\" \"symbol\" nil \"r\") prog1(if(schematic&&symbol t nil) progn(when(schematic dbClose(schematic)) when(symbol dbClose(symbol)))))'%(library,result_cell,library,result_cell),timeout=30)
    exists=not getattr(bridge,'errors',None) and (getattr(bridge,'output','') or '').strip().strip('\"').lower()=='t'
   elif hasattr(self._applier,'cell_exists'):
    exists=self._applier.cell_exists(library,result_cell)
   else: exists=False
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
def _parameter(r): return ParameterSpec(name=r['name'],target=r['target'],lower=r['lower'],upper=r['upper'],dtype=r.get('dtype','float'),scale=r.get('scale','linear'),step=r.get('step'),instance=r.get('instance'),linked_instances=tuple(r.get('linked_instances',())),property=r.get('property'),variable=r.get('variable'),stimulus=r.get('stimulus'),unit=r.get('unit'),sync_property=r.get('sync_property'),sync_factor=r.get('sync_factor'))
def _spec(r): return Spec(metric=r['metric'],op=r['op'],value=r.get('value'),lower=r.get('lower'),upper=r.get('upper'),weight=r.get('weight',1),hard=r.get('hard',False),tolerance=r.get('tolerance',0))
def _spec_eval(specs):
 def call(metrics):
  summary=evaluate_specs(metrics,specs); return {'objective':summary.total,'passed':summary.passed,'results':{x.spec.metric:{'passed':x.passed,'violation':x.violation} for x in summary.results}}
 return call

def _build_runtime_adapters(client,config,specs,run_dir,profile=None):
 from sim_io.site_config import SiteConfig
 from sim_io.sim.run import export_netlist,run_spectre
 from sim_io.sim.config import resolve_sim_config
 site=SiteConfig.from_env()
 source_tb=profile.testbench_cell if profile is not None else config.design.testbench_cell
 dut_instance=profile.dut_instance if profile is not None else config.design.dut_instance
 analyses=[dict(item,profile_id=profile.id) for item in profile.analyses] if profile is not None else config.analyses
 netlist=NetlistAdapter(client,site,library=config.design.library,source_tb=source_tb,work_cell=config.design.work_cell,dut_instance=dut_instance,exporter=export_netlist,base_deck_factory=lambda **k:resolve_sim_config(run_dir=run_dir,lib=k['library'],cell=k['cell']),corner_patcher=lambda deck,corner:patch_smic180_corner(deck,corner,core_model_include=site.pdk_core_spectre_include)); netlist.analyses=analyses
 runner=AnalysisRunner(lambda path,directory:run_spectre(path,directory,site=site,client=client))
 return VirtuosoApplier(client),netlist,runner,MetricsAdapter(analyses)

def _pvt_settings(config):
 raw=dict(config.pvt); explicit=bool(raw.get('voltages'))
 voltage_stimulus=raw.get('voltage_stimulus')
 if explicit:
  voltages=tuple(raw['voltages'])
 else:
  nominal=None
  for name,item in config.stimuli.items():
   kind=item.get('kind') if isinstance(item,Mapping) else getattr(item,'kind',None)
   value=_stim_record(item)['value']
   if kind=='voltage' and value is not None:
    voltage_stimulus=name; nominal=float(value); break
  voltages=(nominal if nominal is not None else 1.0,)
 return PvtConfig(tuple(raw.get('corners',('TT',))),voltages,tuple(raw.get('temperatures',raw.get('temperatures_c',(25.,))))),voltage_stimulus,explicit

class _ReadbackOnlyApplier:
 def __init__(self,applier): self.applier=applier
 def apply_cdf(self,*args,**kwargs): return None
 def __getattr__(self,name): return getattr(self.applier,name)

def create_workflow(config,run_dir):
 client=_load_client_class().from_env(); specs=tuple(_parameter(x) for x in config.parameters); holder={}; root=Path(run_dir)
 profiles=tuple(config.verification_profiles)
 legacy=len(profiles)==1 and profiles[0].id=='default' and profiles[0].role=='legacy'
 if legacy:
  declarations=tuple(_spec(x) for x in config.specs)
  raw,netlist,runner,metrics=_build_runtime_adapters(client,config,specs,run_dir)
  applier=PublicationAdapter(raw,run_dir,specs,lambda:holder.get('candidate',{}))
  backend=AnalogSimulationBackend(config.design.library,config.design.work_cell,specs,config.stimuli,config.analyses,declarations,applier=applier,netlist=netlist,runner=runner,metric_extractor=metrics,spec_evaluator=_spec_eval(declarations))
 else:
  built=[]
  for profile in profiles:
   raw,netlist,runner,metrics=_build_runtime_adapters(client,config,specs,run_dir,profile=profile)
   declarations=tuple(_spec(x) for x in profile.specs)
   profile_analyses=tuple(dict(item,profile_id=profile.id) for item in profile.analyses)
   profile_backend=AnalogSimulationBackend(config.design.library,config.design.work_cell,specs,profile.stimuli,profile_analyses,declarations,applier=_ReadbackOnlyApplier(raw),netlist=netlist,runner=runner,metric_extractor=metrics,spec_evaluator=_spec_eval(declarations))
   built.append((profile,raw,profile_backend))
  applier=PublicationAdapter(built[0][1],run_dir,specs,lambda:holder.get('candidate',{}))
  cdf_specs=tuple(spec for spec in specs if spec.target=='virtuoso_cdf')
  expected={spec.name for spec in specs}
  def apply_candidate(candidate):
   if set(candidate)!=expected: raise ValueError('candidate parameters must exactly match configuration')
   holder['candidate']=dict(candidate)
   if cdf_specs: applier.apply_cdf(config.design.library,config.design.work_cell,cdf_specs,{spec.name:candidate[spec.name] for spec in cdf_specs})
  runtimes=[]
  for profile,raw,profile_backend in built:
   def call(candidate,directory,conditions,profile_backend=profile_backend):
    return profile_backend(candidate,directory,conditions)
   runtimes.append(ProfileRuntime(profile.id,call,required=True))
  backend=MultiProfileBackend(apply_candidate,runtimes)
 evaluator=CandidateEvaluator(backend); space=ParameterSpace(specs); search=SearchConfig(config.search.get('method','random'),config.search.get('evaluations',20),config.search.get('seed',0)); pvt,voltage_stimulus,pvt_voltage_override=_pvt_settings(config)
 def evaluate(candidate,directory,conditions=None):
  holder['candidate']=dict(candidate); directory.mkdir(parents=True,exist_ok=True); raw_result=backend(candidate,directory,conditions or {})
  return EvaluationResult(directory.name,raw_result['objective'],raw_result.get('success',True),raw_result.get('metrics',{}),raw_result.get('metadata',{}),raw_result.get('failure'),raw_result.get('specs',{}))
 def pvt_eval(point,candidate,directory):
  conditions={'corner':point.corner,'temperature':point.temperature}
  if pvt_voltage_override: conditions.update(voltage=point.voltage,voltage_stimulus=voltage_stimulus)
  return evaluate(candidate,directory,conditions)
 return OptimizationWorkflow(root,config_payload=canonical_resolved_payload(config),library=config.design.library,source_cell=config.design.cell,work_cell=config.design.work_cell,result_cell=config.design.result_cell,parameter_specs=specs,applier=applier,evaluator=evaluator,search_config=search,search_runner=lambda resume:run_search(root,space,evaluator,search,resume=resume),replay=evaluate,pvt_config=pvt,pvt_evaluator=pvt_eval)

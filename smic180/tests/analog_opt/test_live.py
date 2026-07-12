import math
from pathlib import Path
import pytest
from analog_opt.live import NetlistAdapter, MetricsAdapter, PublicationAdapter
from analog_opt.parameters import ParameterSpec

class Result:
 def __init__(self,output='OK',errors=()): self.output=output; self.errors=errors
def tb_result(skill):
 if 'ANALOG_OPT_TB_SNAPSHOT' in skill: return Result('"ANALOG_OPT_TB_SNAPSHOT|((2.5 0.0) \"R0\" 1.0)|nil|nil"')
 for marker in ('ANALOG_OPT_TB_COPY_OK','ANALOG_OPT_TB_DELETE_DUT_OK','ANALOG_OPT_TB_CREATE_DUT_OK','ANALOG_OPT_TB_RESTORE_PROPS_OK','ANALOG_OPT_TB_RESTORE_CDF_OK','ANALOG_OPT_TB_DELETE_OK','ANALOG_OPT_TB_OK'):
  if marker in skill: return Result('"'+marker+'"')
 return Result('OK')
class Client:
 def __init__(self): self.skills=[]
 def execute_skill(self,skill,timeout=30):
  self.skills.append(skill)
  return tb_result(skill)
class Site: pass

def test_testbench_step_failure_cleans_copied_cell(tmp_path):
 class C:
  def __init__(self): self.skills=[]
  def execute_skill(self,skill,timeout=30):
   self.skills.append(skill)
   if 'ANALOG_OPT_TB_CREATE_DUT_OK' in skill: return Result('',('create failed',))
   return tb_result(skill)
 client=C(); adapter=NetlistAdapter(client,Site(),library='tr',source_tb='tb',work_cell='work',exporter=lambda *a:None,base_deck_factory=lambda **k:None)
 with pytest.raises(RuntimeError,match='create failed'): adapter._prepare_tb()
 assert 'dbDeleteCellView' in client.skills[-1] and 'ANALOG_OPT_TB_DELETE_OK' in client.skills[-1]


def test_testbench_skill_avoids_single_huge_source_line(tmp_path):
 class C:
  def __init__(self): self.skill=""
  def execute_skill(self,skill,timeout=30): self.skill=skill; return tb_result(skill)
 client=C(); adapter=NetlistAdapter(client,Site(),library="tr",source_tb="tb",work_cell="work",exporter=lambda *a:None,base_deck_factory=lambda **k:None)
 adapter._prepare_tb()
 assert max(map(len,client.skill.splitlines())) < 1000


def test_testbench_creation_reports_bridge_error(tmp_path):
 class C:
  def execute_skill(self,skill,timeout=30): return Result("", ("real skill failure",))
 adapter=NetlistAdapter(C(),Site(),library="tr",source_tb="tb",work_cell="work",exporter=lambda *a:None,base_deck_factory=lambda **k:None)
 with pytest.raises(RuntimeError,match="real skill failure"):
  adapter._prepare_tb()


def test_testbench_skill_uses_file_channel_and_return_sentinel(tmp_path):
 class C:
  def __init__(self): self.skills=[]
  def execute_skill(self,skill,timeout=30):
   self.skills.append(skill)
   return tb_result(skill)
 client=C(); adapter=NetlistAdapter(client,Site(),library="tr",source_tb="tb",work_cell="work",exporter=lambda *a:None,base_deck_factory=lambda **k:None)
 tb=adapter._prepare_tb()
 assert tb.startswith("tb__analog_opt_")
 assert client.skills[0].startswith("progn(\n")
 assert all(skill.startswith("progn(\n") for skill in client.skills)
 assert '"ANALOG_OPT_TB_OK"' in client.skills[-1]


def test_netlist_adapter_builds_dedicated_tb_with_work_cell_dut(tmp_path):
 client=Client(); exported=tmp_path/'raw.scs'
 exported.write_text('subckt amp_work IN OUT\nM1 (OUT IN 0 0) nch w=1e-5 l=1e-6\nends amp_work\nDUT (VIN VOUT) amp_work\nSRC_VDD (VDD 0) vsource dc=3.3\n')
 calls=[]
 adapter=NetlistAdapter(client,Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda c,l,t,d,site:calls.append((l,t)) or exported,base_deck_factory=lambda **k:type('D',(),{'model_includes':[]})())
 adapter.analyses=[{'name':'op','type':'dc_op'}]; adapter.configure({}, {}, {'VDD':{'source_instance':'SRC_VDD','value':3.3}}, {})
 deck=adapter.export_fresh('tr','amp_work',tmp_path/'run')
 assert len(calls)==1 and calls[0][0]=='tr' and calls[0][1].startswith('amp_tb__analog_opt_')
 joined='\n'.join(client.skills)
 assert 'DUT' in joined and 'amp_work' in joined
 deck=deck['op']; text=deck.read_text(); assert 'subckt amp_work' in text and 'DUT (VIN VOUT) amp_work' in text

def test_netlist_adapter_applies_corner_temperature_and_voltage_source(tmp_path):
 raw=tmp_path/'raw.scs'; raw.write_text('subckt amp_work A\nends amp_work\nDUT (A) amp_work\nSUPPLY_MAIN (VDD 0) vsource type=dc dc=3.3\n')
 deck_cfg=type('D',(),{'model_includes':[type('M',(),{'path':'models.scs','section':'tt'})()]})()
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:raw,base_deck_factory=lambda **k:deck_cfg,corner_patcher=lambda d,c:type('D',(),{'model_includes':[type('M',(),{'path':'models.scs','section':c.lower()})()]})())
 adapter.analyses=[{'name':'op','type':'dc_op'}]; adapter.configure({}, {}, {'VDD':{'source_instance':'SUPPLY_MAIN','value':3.3}}, {'corner':'FF','temperature':-40.,'voltage':3.0,'voltage_stimulus':'VDD'})
 deck=adapter.export_fresh('tr','amp_work',tmp_path/'run')['op']; text=deck.read_text()
 assert 'include "models.scs" section=ff' in text
 assert 'temp=-40' in text and 'SUPPLY_MAIN (VDD 0) vsource type=dc dc=3' in text
 confirmed=adapter.confirm({'op':deck},{'op':{'VDD':3.0,'temperature':-40.0,'corner':'FF','dut_cell':'amp_work'}})
 assert confirmed=={'op':{'VDD':3.0,'temperature':-40.0,'corner':'FF','dut_cell':'amp_work'}}

def test_netlist_confirm_extracts_cdf_values_from_work_subckt(tmp_path):
 deck=tmp_path/'deck.scs'; deck.write_text('subckt amp_work A B\nM1 (A B 0 0) nch w=1e-5 l=1e-6\nends amp_work\nDUT (A B) amp_work\n')
 specs=[ParameterSpec('W','virtuoso_cdf',1e-6,2e-5,instance='M1',property='w',unit='m')]
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=None,base_deck_factory=None)
 assert adapter.confirm_cdf(deck,specs)=={'W':1e-5}

def test_metrics_adapter_preserves_curves_and_extracts_task6_metrics():
 data={'freq':[1.,10.,100.],'ac:VOUT':[10+0j,1+0j,.1+0j],'noise:VOUT':[1e-9,2e-9,3e-9],'time':[0.,1e-6,2e-6],'VOUT':[0.,1.1,1.0],'op:M1':{'gm':1e-3,'id':1e-4,'gds':1e-5,'vds':1.2,'vdsat':.2},'VDD_SWEEP':[2.7,3.0,3.3],'dc:VOUT':[1.0,1.1,1.2]}
 result=type('R',(),{'ok':True,'data':data})()
 plan=[{'name':'ac_main','type':'ac','signal':'VOUT'},{'name':'onoise','type':'noise','signal':'VOUT'},{'name':'step','type':'tran','signal':'VOUT','target':1.0},{'name':'op','type':'dc_op','instances':['M1']},{'name':'line','type':'dc_sweep','parameter':'VDD_SWEEP','signal':'VOUT'}]
 metrics=MetricsAdapter(plan)({item['name']:result for item in plan})
 assert metrics['op.M1.gm_over_id']==10.0
 assert 'ac.ac_main.bandwidth_3db_hz' in metrics and 'noise.onoise.integrated_output_vrms' in metrics
 assert metrics['curves']['ac_main']['response']==[[10.0,0.0],[1.0,0.0],[0.1,0.0]]
 assert metrics['curves']['line']['x']==data['VDD_SWEEP']

def test_publication_confirmation_reads_result_cdf_and_cell_exists(tmp_path):
 class A:
  def cell_exists(self,lib,cell): return True
  def read_cdf(self,lib,cell,specs): return {'W':1e-5}
 specs=[ParameterSpec('W','virtuoso_cdf',1e-6,2e-5,instance='M1',property='w',unit='m')]
 adapter=PublicationAdapter(A(),tmp_path,specs,lambda:{'W':1e-5})
 assert adapter.confirm_result_cell('tr','amp_opt','hash') is True
 class Bad(A):
  def read_cdf(self,*a): return {'W':1.1e-5}
 assert PublicationAdapter(Bad(),tmp_path,specs,lambda:{'W':1e-5}).confirm_result_cell('tr','amp_opt','hash') is False
def test_metrics_complex_curve_is_strict_json_serializable():
 import json
 data={'freq':[1.,10.],'ac:VOUT':[1+2j,3-4j]}
 result=type('R',(),{'ok':True,'data':data})()
 metrics=MetricsAdapter([{'name':'ac_main','type':'ac','signal':'VOUT'}])({'ac_main':result})
 json.dumps(metrics,allow_nan=False)
 assert metrics['curves']['ac_main']['response']==[[1.0,2.0],[3.0,-4.0]]

def test_corner_confirmation_reads_deck_not_requested_condition(tmp_path):
 deck=tmp_path/'deck.scs'; deck.write_text('include "models.scs" section=ss\nsubckt amp_work A\nends amp_work\nDUT (A) amp_work\n')
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=None,base_deck_factory=None); adapter.conditions={'corner':'FF'}
 assert adapter.confirm({'op':deck},{'op':{'corner':'SS'}})['op']['corner']=='SS'

def test_publication_adapter_queries_applier_client_when_no_cell_exists_method(tmp_path):
 class BridgeResult:
  output='t'; errors=[]
 class Bridge:
  def execute_skill(self,skill,timeout=30): return BridgeResult()
 class A:
  client=Bridge()
  def read_cdf(self,lib,cell,specs): return {'W':1e-5}
 specs=[ParameterSpec('W','virtuoso_cdf',1e-6,2e-5,instance='M1',property='w',unit='m')]
 assert PublicationAdapter(A(),tmp_path,specs,lambda:{'W':1e-5}).confirm_result_cell('tr','amp_opt','hash') is True

def test_analysis_runner_runs_each_analysis_in_isolated_directory(tmp_path):
 from analog_opt.live import AnalysisRunner
 calls=[]
 def run(deck,directory):
  calls.append((deck.name,directory.name)); return type('R',(),{'ok':True,'data':{'freq':[1.],directory.name:[1.]}})()
 runner=AnalysisRunner(run)
 decks={'ac_main':tmp_path/'ac.scs','onoise':tmp_path/'noise.scs'}
 results=runner.run(decks,tmp_path,[{'name':'ac_main','type':'ac'},{'name':'onoise','type':'noise'}])
 assert calls==[('ac.scs','ac_main'),('noise.scs','onoise')]
 assert set(results)=={'ac_main','onoise'}

def test_metrics_adapter_requires_real_mos_op_data():
 from analog_opt.analyses import AnalysisError
 result=type('R',(),{'ok':True,'data':{}})()
 with pytest.raises(AnalysisError,match='operating-point'):
  MetricsAdapter([{'name':'op','type':'dc_op','instances':['M1']}])({'op':result})

def test_export_builds_one_deck_per_analysis_and_parameterizes_dc_source(tmp_path):
 raw=tmp_path/'raw.scs'; raw.write_text('subckt amp_work A\nends amp_work\nDUT (A) amp_work\nSRC_VDD (VDD 0) vsource type=dc dc=3.3\n')
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:raw,base_deck_factory=lambda **k:type('D',(),{'model_includes':[]})())
 adapter.analyses=[{'name':'line','type':'dc_sweep','source':'VDD','parameter':'VDD_SWEEP','start':2.7,'stop':3.6,'points':10},{'name':'ac_main','type':'ac','start':1.,'stop':1e6,'points_per_decade':10}]
 adapter.configure({}, {}, {'VDD':{'source_instance':'SRC_VDD','value':3.3}}, {})
 decks=adapter.export_fresh('tr','amp_work',tmp_path/'run')
 assert set(decks)=={'line','ac_main'}
 assert 'dc=VDD_SWEEP' in decks['line'].read_text()
 assert 'line dc param=VDD_SWEEP' in decks['line'].read_text() and 'ac_main ac' not in decks['line'].read_text()
 assert 'ac_main ac' in decks['ac_main'].read_text()

def test_source_parser_handles_continuation_case_and_expression(tmp_path):
 raw=tmp_path/'raw.scs'; raw.write_text('subckt amp_work A\nends amp_work\nDUT (A) amp_work\nsupply_main (VDD 0) VSOURCE type=dc \\\n  dc=(3.0+0.3)\n')
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:raw,base_deck_factory=lambda **k:type('D',(),{'model_includes':[]})())
 adapter.analyses=[{'name':'op','type':'dc_op'}]; adapter.configure({}, {}, {'VDD':{'source_instance':'SUPPLY_MAIN','value':3.3}}, {})
 deck=adapter.export_fresh('tr','amp_work',tmp_path/'run')['op']
 assert adapter.confirm({'op':deck},{'op':{'VDD':3.3}})=={'op':{'VDD':3.3}}

def test_mixed_corner_patches_only_core_mos_sections():
 from analog_opt.live import patch_smic180_corner
 models=[type('M',(),{'path':'core.scs','section':'tt'})(),type('M',(),{'path':'bjt.scs','section':'bjt_tt'})(),type('M',(),{'path':'res.scs','section':'res_tt'})()]
 deck=type('D',(),{'model_includes':models})()
 patched=patch_smic180_corner(deck,'fnsp')
 assert [m.section for m in patched.model_includes]==['fnsp','bjt_tt','res_tt']

def test_confirm_cdf_parses_units_and_requires_complete_expected_keys(tmp_path):
 deck=tmp_path/'deck.scs'; deck.write_text('subckt amp_work A B\nM1 (A B 0 0) nch w=10u \\\n l=180n\nends amp_work\nDUT (A B) amp_work\n')
 specs=[ParameterSpec('W','virtuoso_cdf',1e-6,2e-5,instance='M1',property='w',unit='m'),ParameterSpec('L','virtuoso_cdf',1e-7,1e-6,instance='M1',property='l',unit='m')]
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=None,base_deck_factory=None)
 values=adapter.confirm_cdf(deck,specs); assert values['W']==pytest.approx(10e-6) and values['L']==pytest.approx(180e-9)
 deck.write_text('subckt amp_work A B\nM1 (A B 0 0) nch w=10u\nends amp_work\nDUT (A B) amp_work\n')
 with pytest.raises(ValueError,match='complete'): adapter.confirm_cdf(deck,specs)

def test_noise_uses_its_own_frequency_axis():
 result=type('R',(),{'ok':True,'data':{'freq':[1.,10.],'noise_freq':[100.,1000.],'noise:VOUT':[1e-9,2e-9]}})()
 metrics=MetricsAdapter([{'name':'onoise','type':'noise','signal':'VOUT'}])({'onoise':result})
 assert metrics['curves']['onoise']['frequency']==[100.,1000.]

def test_publication_without_cdf_requires_matching_intent_and_hash_marker(tmp_path):
 class A:
  def cell_exists(self,lib,cell): return True
 candidate={'GAIN':4.0}; (tmp_path/'publication.json').write_text('{"candidate_hash":"abc","parameters":{"GAIN":4.0}}'); (tmp_path/'publication.confirmed.json').write_text('{"candidate_hash":"abc"}')
 adapter=PublicationAdapter(A(),tmp_path,[],lambda:candidate)
 assert adapter.confirm_result_cell('tr','amp_opt','abc') is True
 (tmp_path/'publication.confirmed.json').write_text('{"candidate_hash":"wrong"}')
 assert adapter.confirm_result_cell('tr','amp_opt','abc') is False

def test_publication_with_cdf_requires_complete_readback(tmp_path):
 class A:
  def cell_exists(self,lib,cell): return True
  def read_cdf(self,lib,cell,specs): return {}
 specs=[ParameterSpec('W','virtuoso_cdf',1e-6,2e-5,instance='M1',property='w',unit='m')]
 assert PublicationAdapter(A(),tmp_path,specs,lambda:{'W':1e-5}).confirm_result_cell('tr','amp_opt','abc') is False

def test_publication_write_creates_hash_marker_for_noncdf(tmp_path):
 class A:
  def publish_result_cell(self,*args): pass
 (tmp_path/'publication.json').write_text('{"candidate_hash":"abc","parameters":{"GAIN":4.0}}')
 adapter=PublicationAdapter(A(),tmp_path,[],lambda:{'GAIN':4.0}); adapter.publish_result_cell('tr','w','r','s',False)
 assert __import__('json').loads((tmp_path/'publication.confirmed.json').read_text())['candidate_hash']=='abc'

def test_dedicated_tb_name_is_unique_and_skill_closes_all_views(tmp_path):
 client=Client(); adapter=NetlistAdapter(client,Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:None,base_deck_factory=lambda **k:None)
 first=adapter._prepare_tb(); second=adapter._prepare_tb()
 assert first!=second and first.startswith('amp_tb__analog_opt_')
 assert len(client.skills)==14
 for skill in client.skills:
  assert skill.startswith('progn(\n') and 'ANALOG_OPT_TB_' in skill
 joined='\n'.join(client.skills)
 assert 'DUT must be unique' in joined
 assert 'dbCreateInst' in joined and 'dbDeleteObject(dut)' in joined

def test_dedicated_tb_copies_properties_and_cdf_without_sharing_inst_header(tmp_path):
 client=Client(); adapter=NetlistAdapter(client,Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:None,base_deck_factory=lambda **k:None)
 adapter._prepare_tb(); joined='\n'.join(client.skills)
 assert 'dut~>prop' in joined and 'dbCreateProp(dut' in joined
 assert 'cdfGetInstCDF(dut)~>parameters' in joined
 assert 'instHeader~>cdfData' not in joined
 assert 'schCheck(cv)' in joined and 'dbSave(cv)' in joined

def test_live_factory_uses_safe_mixed_corner_patcher_source():
 import inspect,analog_opt.live as live
 source=inspect.getsource(live._build_runtime_adapters)
 assert 'corner_patcher=lambda' in source and 'core_model_include=site.pdk_core_spectre_include' in source

def test_dc_op_deck_requests_real_device_oppoint_output(tmp_path):
 raw=tmp_path/'raw.scs'; raw.write_text('subckt amp_work A\nM1 (A 0 0 0) nch w=10u l=180n\nends amp_work\nDUT (A) amp_work\n')
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:raw,base_deck_factory=lambda **k:type('D',(),{'model_includes':[]})())
 adapter.analyses=[{'name':'op','type':'dc_op','instances':['M1']}]; adapter.configure({}, {}, {}, {})
 text=adapter.export_fresh('tr','amp_work',tmp_path/'run')['op'].read_text()
 assert 'save DUT.M1:oppoint' in text and 'what=oppoint' in text

def test_oppoint_fixture_flows_from_loader_to_metrics(tmp_path):
 from sim_io.sim.run import _load_primary_psf_data
 raw=tmp_path/'deck.raw'; raw.mkdir(); deck=tmp_path/'deck.scs'; deck.write_text('// deck')
 (raw/'op.dcOp').write_text('HEADER\n"analysis type" "dcOp"\nVALUE\n"M1:gm" 1e-3\n"M1:id" 1e-4\n"M1:gds" 1e-5\n"M1:vds" 1.2\n"M1:vdsat" .2\nEND\n')
 data=_load_primary_psf_data(tmp_path,deck); result=type('R',(),{'ok':True,'data':data})()
 metrics=MetricsAdapter([{'name':'op','type':'dc_op','instances':['M1']}])({'op':result})
 assert metrics['op.M1.gm_over_id']==pytest.approx(10.0)


def test_dc_sweep_deck_only_parameterizes_its_own_source_and_saves_hierarchical_op(tmp_path):
 raw=tmp_path/'raw.scs'; raw.write_text('subckt amp_work A B\nM1 (A B 0 0) nch\nends amp_work\nDUT (A B) amp_work\nSRC_VDD (A 0) vsource dc=3.3\nSRC_VBIAS (B 0) vsource dc=1.2\n')
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:raw,base_deck_factory=lambda **k:type('D',(),{'model_includes':[]})())
 adapter.analyses=[{'name':'vdd','type':'dc_sweep','source':'VDD','parameter':'VDD_SWEEP','start':2.7,'stop':3.6,'points':3},{'name':'vbias','type':'dc_sweep','source':'VBIAS','parameter':'VBIAS_SWEEP','start':1.0,'stop':1.4,'points':3},{'name':'op','type':'dc_op','instances':['M1']}]
 adapter.configure({}, {}, {'VDD':{'source_instance':'SRC_VDD','value':3.3},'VBIAS':{'source_instance':'SRC_VBIAS','value':1.2}}, {})
 decks=adapter.export_fresh('tr','amp_work',tmp_path/'run')
 vdd=decks['vdd'].read_text(); vbias=decks['vbias'].read_text(); op=decks['op'].read_text()
 assert 'dc=VDD_SWEEP' in vdd and 'dc=VBIAS_SWEEP' not in vdd
 assert 'dc=VBIAS_SWEEP' in vbias and 'dc=VDD_SWEEP' not in vbias
 assert 'save DUT.M1:oppoint' in op


def test_mixed_corner_replaces_only_core_tt_sections():
 models=[type('M',(),{'path':'core.scs','section':'tt_core'})(),type('M',(),{'path':'passive.scs','section':'tt_res'})()]
 deck=type('D',(),{'model_includes':models})()
 patched=__import__('analog_opt.live',fromlist=['patch_smic180_corner']).patch_smic180_corner(deck,'FNSP')
 assert [(m.path,m.section) for m in patched.model_includes]==[('core.scs','fnsp_core'),('passive.scs','tt_res')]


def test_metrics_adapter_validates_dc_curve_and_writes_svg(tmp_path):
 result=type('R',(),{'ok':True,'data':{'VDD_SWEEP':[2.7,3.0,3.3],'dc:VOUT':[1.0,1.1,1.2]},'metadata':{'run_dir':str(tmp_path)}})()
 metrics=MetricsAdapter([{'name':'line','type':'dc_sweep','parameter':'VDD_SWEEP','signal':'VOUT','points':3}])({'line':result})
 svg=tmp_path/'line'/'dc_line.svg'
 assert svg.exists() and '<path' in svg.read_text(encoding='utf-8')
 assert metrics['artifacts']['line']['svg'].endswith('dc_line.svg')

@pytest.mark.parametrize('x,y',[(None,[1,2]),([1],[1]),([1,2],[1]),([1,float('nan')],[1,2])])
def test_metrics_adapter_rejects_invalid_dc_curve(tmp_path,x,y):
 result=type('R',(),{'ok':True,'data':{'VDD_SWEEP':x,'dc:VOUT':y},'metadata':{'run_dir':str(tmp_path)}})()
 with pytest.raises((RuntimeError,ValueError)):
  MetricsAdapter([{'name':'line','type':'dc_sweep','parameter':'VDD_SWEEP','signal':'VOUT','points':2}])({'line':result})


def test_dedicated_tb_is_deleted_after_export(tmp_path):
 client=Client(); raw=tmp_path/'raw.scs'; raw.write_text('subckt amp_work A\nends amp_work\nDUT (A) amp_work\n')
 adapter=NetlistAdapter(client,Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:raw,base_deck_factory=lambda **k:type('D',(),{'model_includes':[]})())
 adapter.analyses=[{'name':'op','type':'dc_op'}]; adapter.configure({}, {}, {}, {})
 adapter.export_fresh('tr','amp_work',tmp_path/'run')
 assert len(client.skills)==8 and 'dbDeleteCellView' in client.skills[-1] and 'ANALOG_OPT_TB_DELETE_OK' in client.skills[-1]


def test_empty_pvt_uses_fixed_nominal_voltage_without_override():
 from analog_opt.live import _pvt_settings
 cfg=type('C',(),{'pvt':{'corners':['TT'],'voltages':[],'temperatures_c':[25.0]},'stimuli':{'VDD':type('S',(),{'kind':'voltage','value':3.3,'dc':None})()}})()
 pvt,voltage_stimulus,override=_pvt_settings(cfg)
 assert pvt.voltages==(3.3,) and voltage_stimulus=='VDD' and override is False


def test_mixed_corner_final_deck_replaces_exported_core_include(tmp_path):
 raw=tmp_path/'raw.scs'; raw.write_text('include "core.scs" section=tt_core\ninclude "passive.scs" section=tt_res\nsubckt amp_work A\nends amp_work\nDUT (A) amp_work\n')
 cfg=type('D',(),{'model_includes':[type('M',(),{'path':'core.scs','section':'tt_core'})(),type('M',(),{'path':'passive.scs','section':'tt_res'})()]})()
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:raw,base_deck_factory=lambda **k:cfg,corner_patcher=__import__('analog_opt.live',fromlist=['patch_smic180_corner']).patch_smic180_corner)
 adapter.analyses=[{'name':'op','type':'dc_op'}]; adapter.configure({}, {}, {}, {'corner':'FNSP'})
 text=adapter.export_fresh('tr','amp_work',tmp_path/'run')['op'].read_text()
 assert text.count('core.scs')==1 and 'section=fnsp_core' in text and 'section=tt_core' not in text
 assert text.count('passive.scs')==1 and 'section=tt_res' in text


def test_mixed_corner_uses_explicit_real_site_core_include_identity():
 from analog_opt.live import patch_smic180_corner
 core='/home/IC/pdk/smic180/models/e2r018_v1p8_spe.scs'
 models=[type('M',(),{'path':core,'section':'tt'})(),type('M',(),{'path':'/home/IC/pdk/passive.scs','section':'tt'})()]
 deck=type('D',(),{'model_includes':models})()
 patched=patch_smic180_corner(deck,'FNSP',core_model_include=core)
 assert [(m.path,m.section) for m in patched.model_includes]==[(core,'fnsp'),('/home/IC/pdk/passive.scs','tt')]

def test_runtime_adapter_binds_site_core_model_identity():
 import inspect
 from analog_opt.live import _build_runtime_adapters
 source=inspect.getsource(_build_runtime_adapters)
 assert 'site.pdk_core_spectre_include' in source and 'core_model_include' in source

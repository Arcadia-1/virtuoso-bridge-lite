import math
from pathlib import Path
import pytest
from analog_opt.live import NetlistAdapter, MetricsAdapter, PublicationAdapter
from analog_opt.parameters import ParameterSpec

class Result:
 def __init__(self,output='OK',errors=()): self.output=output; self.errors=errors
class Client:
 def __init__(self): self.skills=[]
 def execute_skill(self,skill,timeout=30): self.skills.append(skill); return Result('ANALOG_OPT_TB_OK')
class Site: pass

def test_netlist_adapter_builds_dedicated_tb_with_work_cell_dut(tmp_path):
 client=Client(); exported=tmp_path/'raw.scs'
 exported.write_text('subckt amp_work IN OUT\nM1 (OUT IN 0 0) nch w=1e-5 l=1e-6\nends amp_work\nDUT (VIN VOUT) amp_work\nSRC_VDD (VDD 0) vsource dc=3.3\n')
 calls=[]
 adapter=NetlistAdapter(client,Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda c,l,t,d,site:calls.append((l,t)) or exported,base_deck_factory=lambda **k:type('D',(),{'model_includes':[]})())
 adapter.configure({}, {}, {'VDD':{'source_instance':'SRC_VDD','value':3.3}}, {})
 deck=adapter.export_fresh('tr','amp_work',tmp_path/'run')
 assert calls==[('tr','amp_tb__analog_opt')]
 assert 'DUT' in client.skills[0] and 'amp_work' in client.skills[0]
 text=deck.read_text(); assert 'subckt amp_work' in text and 'DUT (VIN VOUT) amp_work' in text

def test_netlist_adapter_applies_corner_temperature_and_voltage_source(tmp_path):
 raw=tmp_path/'raw.scs'; raw.write_text('subckt amp_work A\nends amp_work\nDUT (A) amp_work\nSUPPLY_MAIN (VDD 0) vsource type=dc dc=3.3\n')
 deck_cfg=type('D',(),{'model_includes':[type('M',(),{'path':'models.scs','section':'tt'})()]})()
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=lambda *a,**k:raw,base_deck_factory=lambda **k:deck_cfg,corner_patcher=lambda d,c:type('D',(),{'model_includes':[type('M',(),{'path':'models.scs','section':c.lower()})()]})())
 adapter.configure({}, {}, {'VDD':{'source_instance':'SUPPLY_MAIN','value':3.3}}, {'corner':'FF','temperature':-40.,'voltage':3.0,'voltage_stimulus':'VDD'})
 deck=adapter.export_fresh('tr','amp_work',tmp_path/'run'); text=deck.read_text()
 assert 'include "models.scs" section=ff' in text
 assert 'temp=-40' in text and 'SUPPLY_MAIN (VDD 0) vsource type=dc dc=3' in text
 confirmed=adapter.confirm(deck,['VDD','temperature','corner','dut_cell'])
 assert confirmed=={'VDD':3.0,'temperature':-40.0,'corner':'FF','dut_cell':'amp_work'}

def test_netlist_confirm_extracts_cdf_values_from_work_subckt(tmp_path):
 deck=tmp_path/'deck.scs'; deck.write_text('subckt amp_work A B\nM1 (A B 0 0) nch w=1e-5 l=1e-6\nends amp_work\nDUT (A B) amp_work\n')
 specs=[ParameterSpec('W','virtuoso_cdf',1e-6,2e-5,instance='M1',property='w',unit='m')]
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=None,base_deck_factory=None)
 assert adapter.confirm_cdf(deck,specs)=={'W':1e-5}

def test_metrics_adapter_preserves_curves_and_extracts_task6_metrics():
 data={'freq':[1.,10.,100.],'ac:VOUT':[10+0j,1+0j,.1+0j],'noise:VOUT':[1e-9,2e-9,3e-9],'time':[0.,1e-6,2e-6],'VOUT':[0.,1.1,1.0],'op:M1':{'gm':1e-3,'id':1e-4,'gds':1e-5,'vds':1.2,'vdsat':.2},'VDD_SWEEP':[2.7,3.0,3.3],'dc:VOUT':[1.0,1.1,1.2]}
 result=type('R',(),{'ok':True,'data':data})()
 plan=[{'name':'ac_main','type':'ac','signal':'VOUT'},{'name':'onoise','type':'noise','signal':'VOUT'},{'name':'step','type':'tran','signal':'VOUT','target':1.0},{'name':'op','type':'dc_op','instances':['M1']},{'name':'line','type':'dc_sweep','parameter':'VDD_SWEEP','signal':'VOUT'}]
 metrics=MetricsAdapter(plan)(result)
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
 metrics=MetricsAdapter([{'name':'ac_main','type':'ac','signal':'VOUT'}])(result)
 json.dumps(metrics,allow_nan=False)
 assert metrics['curves']['ac_main']['response']==[[1.0,2.0],[3.0,-4.0]]

def test_corner_confirmation_reads_deck_not_requested_condition(tmp_path):
 deck=tmp_path/'deck.scs'; deck.write_text('include "models.scs" section=ss\nsubckt amp_work A\nends amp_work\nDUT (A) amp_work\n')
 adapter=NetlistAdapter(Client(),Site(),library='tr',source_tb='amp_tb',work_cell='amp_work',exporter=None,base_deck_factory=None); adapter.conditions={'corner':'FF'}
 assert adapter.confirm(deck,['corner'])['corner']=='SS'

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

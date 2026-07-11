import importlib,sys
from analog_opt.schema import AnalogOptConfig,DesignConfig,StimulusConfig

def cfg(): return AnalogOptConfig(2,DesignConfig('tr','amp','amp_work','amp_opt','amp_tb'),{'VDD':StimulusConfig('voltage',value=3.3)},[{'name':'W','target':'virtuoso_cdf','lower':1e-6,'upper':2e-6,'instance':'M1','property':'w','unit':'m'}],[],[],[],{'method':'random','evaluations':1,'seed':1},{'corners':['TT'],'voltages':[3.3],'temperatures':[25]}, {'run_dir':'run'})
def test_live_module_import_does_not_import_bridge():
 sys.modules.pop('analog_opt.live',None); sys.modules.pop('virtuoso_bridge',None); importlib.import_module('analog_opt.live'); assert 'virtuoso_bridge' not in sys.modules
def test_create_workflow_connects_lazily_and_assembles_real_signatures(tmp_path,monkeypatch):
 from analog_opt import live
 calls=[]
 class Client:
  @classmethod
  def from_env(cls): calls.append('connect'); return object()
 monkeypatch.setattr(live,'_load_client_class',lambda:Client)
 monkeypatch.setattr(live,'_build_runtime_adapters',lambda client,config:(object(),object(),object(),lambda x:{},lambda x:{'objective':0.,'passed':True,'results':{}}))
 w=live.create_workflow(cfg(),tmp_path)
 assert calls==['connect'] and w.library=='tr' and w.work_cell=='amp_work' and [s.name for s in w.parameter_specs]==['W']
def test_live_factory_supplies_publication_confirmation_and_direct_replay(tmp_path,monkeypatch):
 from analog_opt import live
 class Client:
  @classmethod
  def from_env(cls): return object()
 class A:
  def create_work_cell(self,*a): pass
  def apply_cdf(self,*a): pass
  def read_cdf(self,*a): return {'W':1.5e-6}
  def publish_result_cell(self,*a): pass
 class N: pass
 class R: pass
 monkeypatch.setattr(live,'_load_client_class',lambda:Client)
 monkeypatch.setattr(live,'_build_runtime_adapters',lambda client,config:(A(),N(),R(),lambda x:{},lambda x:{'objective':0.,'passed':True,'results':{}}))
 w=live.create_workflow(cfg(),tmp_path)
 assert callable(w.applier.confirm_result_cell)
 assert callable(w.replay)

from dataclasses import dataclass
import pytest
from analog_opt.apply import ApplyError, VirtuosoApplier
from analog_opt.parameters import ParameterSpec
@dataclass
class Result:
    output: str = "t"
    errors: tuple = ()
class RecordingClient:
    def __init__(self, results=None): self.calls=[]; self.results=list(results or [])
    def execute_skill(self, skill, timeout=30): self.calls.append((skill,timeout)); return self.results.pop(0) if self.results else Result()
def spec(name, inst, prop, unit=None): return ParameterSpec(name=name,target="virtuoso_cdf",lower=1e-9,upper=1.0,instance=inst,property=prop,unit=unit)
def test_apply_batches_and_syncs_fw():
    c=RecordingClient(); VirtuosoApplier(c).apply_cdf("tr","amp_work",[spec("W","M1","w","um"),spec("R","R1","r","kOhm")],{"W":10e-6,"R":2000.0})
    assert len(c.calls)==1; s=c.calls[0][0]
    for text in ['foreach(inst cv~>instances','dbReplaceProp(inst "w" "string" "10um")','dbReplaceProp(inst "fw" "string" "10um")','dbReplaceProp(inst "r" "string" "2kOhm")','schCheck(cv)','dbSave(cv)']: assert text in s
@pytest.mark.parametrize("candidate",[{"W":1e-6},{"W":1e-6,"L":1e-6,"extra":1}])
def test_exact_candidate(candidate):
    with pytest.raises(ApplyError,match="exactly"): VirtuosoApplier(RecordingClient()).apply_cdf("tr","c",[spec("W","M1","w","um"),spec("L","M1","l","um")],candidate)
@pytest.mark.parametrize("bad",[float("nan"),float("inf"),True,"10um"])
def test_bad_values(bad):
    with pytest.raises(ApplyError,match="finite number"): VirtuosoApplier(RecordingClient()).apply_cdf("tr","c",[spec("W","M1","w","um")],{"W":bad})
@pytest.mark.parametrize("field,value",[("library","bad lib"),("cell","bad cell"),("instance","M1)"),("property","w~>x")])
def test_bad_identifiers(field,value):
    x={"library":"tr","cell":"c","instance":"M1","property":"w"}; x[field]=value
    with pytest.raises(ApplyError,match="invalid"): VirtuosoApplier(RecordingClient()).apply_cdf(x["library"],x["cell"],[spec("W",x["instance"],x["property"],"um")],{"W":1e-6})
def test_copy_safety_and_replace():
    a=VirtuosoApplier(RecordingClient())
    with pytest.raises(ApplyError,match="distinct"): a.create_work_cell("tr","amp","amp",False)
    a.create_work_cell("tr","amp","work",True); s=a.client.calls[0][0]; assert "dbCopyCellView" in s and "ddDeleteCell" in s and 'dbOpenCellViewByType("tr" "amp" "schematic" "schematic" "r")' in s and 'dbOpenCellViewByType("tr" "amp" "schematic" "schematic" "a")' not in s
    with pytest.raises(ApplyError,match="already exists"): VirtuosoApplier(RecordingClient([Result("EXISTS")])).create_work_cell("tr","amp","work",False)
def test_read_and_bridge_error():
    c=RecordingClient([Result('(("M1" ("w" "10um")))')]); assert "10um" in VirtuosoApplier(c).read_cdf("tr","work",[spec("W","M1","w","um")]); assert '"r")' in c.calls[0][0]
    with pytest.raises(ApplyError,match="bridge"): VirtuosoApplier(RecordingClient([Result("nil",("boom",))])).read_cdf("tr","work",[spec("W","M1","w","um")])
def test_publish_safety():
    a=VirtuosoApplier(RecordingClient())
    with pytest.raises(ApplyError,match="source"): a.publish_result_cell("tr","work","amp","amp",True)
    a.publish_result_cell("tr","work","best","amp",True); assert "dbCopyCellView" in a.client.calls[0][0]
    with pytest.raises(ApplyError,match="already exists"): VirtuosoApplier(RecordingClient([Result("EXISTS")])).publish_result_cell("tr","work","best","amp",False)
def test_bridge_output_error():
    with pytest.raises(ApplyError,match="bridge"): VirtuosoApplier(RecordingClient([Result("error: failed")])).create_work_cell("tr","amp","work",True)

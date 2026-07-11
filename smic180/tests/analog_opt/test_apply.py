from dataclasses import dataclass
import pytest
from analog_opt.apply import ApplyError, VirtuosoApplier
from analog_opt.parameters import ParameterSpec

@dataclass
class Result:
    output: str = ""
    errors: tuple = ()

class RecordingClient:
    def __init__(self, results): self.results=list(results); self.calls=[]
    def execute_skill(self, skill, timeout=30): self.calls.append((skill,timeout)); return self.results.pop(0)

def spec(name, instance, prop, unit=None, dtype="float", target="virtuoso_cdf", sync_property=None):
    return ParameterSpec(name=name,target=target,lower=0,upper=100,dtype=dtype,instance=instance,property=prop,unit=unit,sync_property=sync_property)

def test_apply_single_transaction_uses_cdf_and_verifies_before_save():
    c=RecordingClient([Result("ANALOG_OPT_OK:apply")]); VirtuosoApplier(c).apply_cdf("tr","work",[spec("W","M1","w","um"),spec("R","R1","r","kOhm")],{"W":10e-6,"R":2000.0})
    s=c.calls[0][0]; assert len(c.calls)==1; assert "unwindProtect" in s and "setInstParams" not in s
    assert "foreach(inst cv~>instances" in s and "cdfGetInstCDF(inst0)" in s and 'param~>name=="w"' in s
    assert 'unless(param0 error("CDF parameter missing: M1.w"))' in s and 'param0~>value="10um"' in s
    assert 'unless(param0~>value=="10um"' in s and s.index("CDF parameter missing") < s.index("param0~>value") < s.index("dbSave(cv)")
    assert "when(schCheck(cv) dbSave(cv)" in s and "when(cv dbClose(cv))" in s and "dbReplaceProp" not in s

def test_explicit_sync_property_uses_db_replace_prop_only_for_sync():
    c=RecordingClient([Result("ANALOG_OPT_OK:apply")]); VirtuosoApplier(c).apply_cdf("tr","work",[spec("W","M1","w","um",sync_property="fw")],{"W":10e-6})
    s=c.calls[0][0]; assert 'dbReplaceProp(inst0 "fw" "string" "10um")' in s; assert 'dbReplaceProp(inst0 "w"' not in s

def test_non_mos_width_without_sync_does_not_write_fw():
    c=RecordingClient([Result("ANALOG_OPT_OK:apply")]); VirtuosoApplier(c).apply_cdf("tr","work",[spec("W","R1","w","um")],{"W":10e-6}); assert '"fw"' not in c.calls[0][0]

def test_apply_protocol_and_integer_validation():
    VirtuosoApplier(RecordingClient([Result("contains error\nANALOG_OPT_OK:apply")])).apply_cdf("tr","work",[spec("M","M1","m",dtype="int")],{"M":4})
    with pytest.raises(ApplyError,match="sentinel"): VirtuosoApplier(RecordingClient([Result("t")])).apply_cdf("tr","work",[spec("M","M1","m",dtype="int")],{"M":4})
    with pytest.raises(ApplyError,match="bridge"): VirtuosoApplier(RecordingClient([Result("ANALOG_OPT_OK:apply",("bad",))])).apply_cdf("tr","work",[spec("M","M1","m",dtype="int")],{"M":4})
    with pytest.raises(ApplyError,match="integer"): VirtuosoApplier(RecordingClient([])).apply_cdf("tr","work",[spec("M","M1","m",dtype="int")],{"M":4.5})

def test_apply_validates_candidate_and_identifiers_before_bridge():
    a=VirtuosoApplier(RecordingClient([]))
    with pytest.raises(ApplyError,match="exactly"): a.apply_cdf("tr","work",[spec("W","M1","w")],{"X":1})
    with pytest.raises(ApplyError,match="virtuoso_cdf"): a.apply_cdf("tr","work",[spec("W","M1","w",target="bias")],{"W":1})
    with pytest.raises(ApplyError,match="invalid"): a.apply_cdf("tr","work",[spec("W","M1)","w")],{"W":1})

def test_copy_closes_all_views_and_only_deletes_temp_view():
    c=RecordingClient([Result("ANALOG_OPT_OK:create:CREATED")]); VirtuosoApplier(c).create_work_cell("tr","amp","work",False); s=c.calls[0][0]
    assert 'dbOpenCellViewByType("tr" "amp" "schematic" "schematic" "r")' in s and "dbCopyCellView" in s
    assert "when(srcCv dbClose(srcCv))" in s and "when(tmpCv dbClose(tmpCv))" in s and "when(dstCv dbClose(dstCv))" in s
    assert "ddDeleteCell" not in s and 'dbDeleteCellView("tr" "work"' not in s and '__analog_opt_tmp' in s

@pytest.mark.parametrize("replace,message",[(False,"already exists"),(True,"safe replacement unsupported")])
def test_create_existing_destination_replace_contract(replace,message):
    with pytest.raises(ApplyError,match=message): VirtuosoApplier(RecordingClient([Result("ANALOG_OPT_OK:create:EXISTS")])).create_work_cell("tr","amp","work",replace)

@pytest.mark.parametrize("work,result,source",[("amp","best","amp"),("work","amp","amp"),("work","work","amp")])
def test_publish_requires_distinct_cells(work,result,source):
    with pytest.raises(ApplyError,match="distinct"): VirtuosoApplier(RecordingClient([])).publish_result_cell("tr",work,result,source,False)

def test_publish_existing_replace_true_is_unsupported():
    with pytest.raises(ApplyError,match="safe replacement unsupported"): VirtuosoApplier(RecordingClient([Result("ANALOG_OPT_OK:publish:EXISTS")])).publish_result_cell("tr","work","best","amp",True)

def test_read_uses_cdf_parameters_and_returns_si_mapping():
    c=RecordingClient([Result("ANALOG_OPT_OK:read\nW\t10um\nM\t4\nR\t2kOhm")]); values=VirtuosoApplier(c).read_cdf("tr","work",[spec("W","M1","w","um"),spec("M","M1","m",dtype="int"),spec("R","R1","r","kOhm")]); s=c.calls[0][0]
    assert values["W"]==pytest.approx(10e-6) and values["M"]==4 and values["R"]==pytest.approx(2000)
    assert "cdfGetInstCDF(inst)~>parameters" in s and 'p~>name=="w"' in s and "param~>value" in s and "getq(inst" not in s and "dbClose(cv)" in s

@pytest.mark.parametrize("output,match",[("ANALOG_OPT_OK:read\nW\t10um\nW\t11um","duplicate"),("ANALOG_OPT_OK:read","missing"),("ANALOG_OPT_OK:read\nW\tnan","finite")])
def test_read_rejects_invalid_machine_output(output,match):
    with pytest.raises(ApplyError,match=match): VirtuosoApplier(RecordingClient([Result(output)])).read_cdf("tr","work",[spec("W","M1","w","um")])

def test_read_protocol_errors():
    with pytest.raises(ApplyError,match="bridge"): VirtuosoApplier(RecordingClient([Result("ANALOG_OPT_OK:read\nW\t10um",("bad",))])).read_cdf("tr","work",[spec("W","M1","w","um")])
    with pytest.raises(ApplyError,match="sentinel"): VirtuosoApplier(RecordingClient([Result("W\t10um")])).read_cdf("tr","work",[spec("W","M1","w","um")])

def test_explicit_sync_property_is_read_back_before_save():
    c = RecordingClient([Result("ANALOG_OPT_OK:apply")])
    VirtuosoApplier(c).apply_cdf("tr", "work", [spec("W", "M1", "w", "um", sync_property="fw")], {"W": 10e-6})
    skill = c.calls[0][0]
    assert 'getq(inst0 stringToSymbol("fw"))=="10um"' in skill
    assert skill.index('getq(inst0 stringToSymbol("fw"))') < skill.index("dbSave(cv)")

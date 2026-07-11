from dataclasses import dataclass

import pytest

from analog_opt.apply import ApplyError, VirtuosoApplier
from analog_opt.parameters import ParameterSpec


@dataclass
class Result:
    output: str = ""
    errors: tuple = ()


class RecordingClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def execute_skill(self, skill, timeout=30):
        self.calls.append((skill, timeout))
        return self.results.pop(0)


def spec(name, instance, prop, unit=None, dtype="float", target="virtuoso_cdf"):
    return ParameterSpec(name=name, target=target, lower=0, upper=100, dtype=dtype, instance=instance, property=prop, unit=unit)


def test_apply_uses_one_checked_transaction_and_structured_success():
    client = RecordingClient([Result("ANALOG_OPT_OK:apply")])
    VirtuosoApplier(client).apply_cdf("tr", "work", [spec("W", "M1", "w", "um"), spec("R", "R1", "r", "kOhm")], {"W": 10e-6, "R": 2000.0})
    assert len(client.calls) == 1
    skill = client.calls[0][0]
    assert "unwindProtect" in skill
    assert "foreach(inst cv~>instances" in skill
    assert "setInstParams" in skill
    assert 'dbReplaceProp(inst0 "w" "string" "10um")' in skill
    assert 'dbReplaceProp(inst1 "r" "string" "2kOhm")' in skill
    assert 'dbReplaceProp(inst "fw"' not in skill
    assert skill.index("instance not found") < skill.index("setInstParams")
    assert "when(schCheck(cv) dbSave(cv)" in skill
    assert 'printf("ANALOG_OPT_OK:apply")' in skill
    assert "dbClose(cv)" in skill


def test_apply_requires_sentinel_but_allows_error_word_in_valid_output():
    VirtuosoApplier(RecordingClient([Result("contains error text\nANALOG_OPT_OK:apply")])).apply_cdf("tr", "work", [spec("M", "M1", "m", dtype="int")], {"M": 4})
    with pytest.raises(ApplyError, match="sentinel"):
        VirtuosoApplier(RecordingClient([Result("t")])).apply_cdf("tr", "work", [spec("M", "M1", "m", dtype="int")], {"M": 4})
    with pytest.raises(ApplyError, match="bridge"):
        VirtuosoApplier(RecordingClient([Result("ANALOG_OPT_OK:apply", ("boom",))])).apply_cdf("tr", "work", [spec("M", "M1", "m", dtype="int")], {"M": 4})


def test_apply_rejects_noninteger_at_int_boundary():
    with pytest.raises(ApplyError, match="integer"):
        VirtuosoApplier(RecordingClient([])).apply_cdf("tr", "work", [spec("M", "M1", "m", dtype="int")], {"M": 4.5})


def test_apply_rejects_candidate_shape_and_invalid_specs_before_bridge():
    applier = VirtuosoApplier(RecordingClient([]))
    with pytest.raises(ApplyError, match="exactly"):
        applier.apply_cdf("tr", "work", [spec("W", "M1", "w")], {"extra": 1})
    with pytest.raises(ApplyError, match="virtuoso_cdf"):
        applier.apply_cdf("tr", "work", [spec("X", "M1", "w", target="bias")], {"X": 1})
    with pytest.raises(ApplyError, match="invalid"):
        applier.apply_cdf("tr", "work", [spec("W", "M1)", "w")], {"W": 1})


@pytest.mark.parametrize("work,result,source", [("amp", "best", "amp"), ("work", "amp", "amp"), ("work", "work", "amp")])
def test_publish_requires_three_distinct_cells(work, result, source):
    with pytest.raises(ApplyError, match="distinct"):
        VirtuosoApplier(RecordingClient([])).publish_result_cell("tr", work, result, source, False)


def test_create_and_publish_never_replace_existing_destination():
    for operation in ("create", "publish"):
        client = RecordingClient([Result("ANALOG_OPT_OK:%s:EXISTS" % operation)])
        applier = VirtuosoApplier(client)
        with pytest.raises(ApplyError, match="already exists"):
            if operation == "create": applier.create_work_cell("tr", "amp", "work", True)
            else: applier.publish_result_cell("tr", "work", "best", "amp", True)
        skill = client.calls[0][0]
        assert "ddDeleteCell" not in skill
        assert "dbDeleteCellView" in skill
        assert "__analog_opt_tmp" in skill
        assert 'dbDeleteCellView("tr" "work"' not in skill and 'dbDeleteCellView("tr" "best"' not in skill


def test_copy_uses_temporary_cell_and_cleans_only_temporary_view():
    client = RecordingClient([Result("ANALOG_OPT_OK:create:CREATED")])
    VirtuosoApplier(client).create_work_cell("tr", "amp", "work", False)
    skill = client.calls[0][0]
    assert 'dbOpenCellViewByType("tr" "amp" "schematic" "schematic" "r")' in skill
    assert "dbCopyCellView" in skill
    assert "unwindProtect" in skill
    assert "dbDeleteCellView" in skill
    assert "ddDeleteCell" not in skill


def test_read_cdf_returns_named_si_mapping_and_integers():
    output = "ANALOG_OPT_OK:read\nW\t10um\nM\t4\nR\t2kOhm"
    client = RecordingClient([Result(output)])
    values = VirtuosoApplier(client).read_cdf("tr", "work", [spec("W", "M1", "w", "um"), spec("M", "M1", "m", dtype="int"), spec("R", "R1", "r", "kOhm")])
    assert values["W"] == pytest.approx(10e-6); assert values["M"] == 4; assert values["R"] == pytest.approx(2000.0)
    skill = client.calls[0][0]
    assert "ANALOG_OPT_OK:read" in skill
    assert "dbGetq" not in skill
    assert "getq" in skill
    assert "dbClose(cv)" in skill


@pytest.mark.parametrize("output,match", [("ANALOG_OPT_OK:read\nW\t10um\nW\t11um", "duplicate"), ("ANALOG_OPT_OK:read", "missing"), ("ANALOG_OPT_OK:read\nW\tnan", "finite")])
def test_read_rejects_malformed_machine_output(output, match):
    with pytest.raises(ApplyError, match=match):
        VirtuosoApplier(RecordingClient([Result(output)])).read_cdf("tr", "work", [spec("W", "M1", "w", "um")])


def test_read_rejects_bridge_errors_and_missing_sentinel():
    with pytest.raises(ApplyError, match="bridge"):
        VirtuosoApplier(RecordingClient([Result("ANALOG_OPT_OK:read\nW\t10um", ("bad",))])).read_cdf("tr", "work", [spec("W", "M1", "w", "um")])
    with pytest.raises(ApplyError, match="sentinel"):
        VirtuosoApplier(RecordingClient([Result("W\t10um")])).read_cdf("tr", "work", [spec("W", "M1", "w", "um")])

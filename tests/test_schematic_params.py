"""set_instance_params resolves shorthands against the instance's CDF."""

from __future__ import annotations

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.schematic.params import set_instance_params

# (prompt, editable) per CDF parameter, as cdfGetInstCDF reports them: kits
# such as TSMC set editable to an expression string ("t"/"nil"); analogLib
# leaves it unset (None), which means editable.
TSMC28_STYLE = {
    "w": ("Total width", "t"), "Wfg": ("Finger width", "t"), "l": ("Length", "t"),
    "fingers": ("Fingers", "t"), "nf": ("Nf", "nil"), "m": ("Multiplier", "t"),
}
EDITABLE_NF = {"w": ("Width", "t"), "l": ("Length", "t"), "nf": ("Fingers", "t"), "m": ("M", "t")}
ANALOG_RES = {"r": ("Resistance", None), "w": ("Width", None), "l": ("Length", None)}
ANALOG_IND = {"l": ("Inductance", None), "r": ("Resistance", None), "m": ("Multiplier", None)}


class FakeClient:
    def __init__(self, cdf, master="pdk|dev"):
        self.cdf = cdf
        self.master = master
        self.skill: list[str] = []

    def execute_skill(self, code, timeout=None, **_kwargs):
        self.skill.append(code)
        if "geGetEditCellView()" in code and "libName" in code:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"lib|cell"')
        if "inst~>libName inst~>cellName" in code:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=f'"{self.master}"')
        if "cdfGetInstCDF(inst)" in code and "p~>prompt" in code:
            rows = " ".join(
                f'("{n}" "{p}" {"nil" if e is None else chr(34) + e + chr(34)})'
                for n, (p, e) in self.cdf.items()
            )
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=f"({rows})")
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")

    def updates(self):
        return [code for code in self.skill if "setarray(paramVals" in code]


@pytest.mark.parametrize(("cdf", "kwargs", "expected"), [
    (TSMC28_STYLE, {"w": "2u", "nf": "4", "m": "2"}, {"w": "2u", "fingers": "4", "m": "2"}),
    (TSMC28_STYLE, {"wf": "500n", "l": "30n"}, {"Wfg": "500n", "l": "30n"}),
    (EDITABLE_NF, {"w": "2u", "nf": "4"}, {"w": "2u", "nf": "4"}),
    (ANALOG_RES, {"r": "10k"}, {"r": "10k"}),
    # analogLib leaves "editable" unset: the inductance "l" must still apply.
    (ANALOG_IND, {"l": "2n", "m": "2"}, {"l": "2n", "m": "2"}),
])
def test_names_resolve_to_existing_editable_cdf_params(cdf, kwargs, expected):
    client = FakeClient(cdf)
    assert set_instance_params(client, "X0", param_filters=None, **kwargs) == expected
    for name, value in expected.items():
        assert f'setarray(paramVals "{name}" "{value}")' in client.updates()[-1]


@pytest.mark.parametrize(("cdf", "kwargs", "message"), [
    (ANALOG_RES, {"bogus": "1"}, r"no CDF parameter\(s\): \['bogus'\]"),
    (ANALOG_RES, {"nf": "2"}, r"no editable CDF parameter for nf= \(tried nf, fingers\)"),
    (EDITABLE_NF, {"wf": "1u"}, r"no editable CDF parameter for wf= \(tried Wfg\); width parameters: w \('Width'\)"),
])
def test_unresolvable_names_are_refused_before_any_update(cdf, kwargs, message):
    client = FakeClient(cdf)
    with pytest.raises(ValueError, match=message):
        set_instance_params(client, "X0", param_filters=None, **kwargs)
    assert client.updates() == []


def test_w_and_wf_together_are_refused_without_contacting_virtuoso():
    client = FakeClient(TSMC28_STYLE)
    with pytest.raises(ValueError, match="not both"):
        set_instance_params(client, "X0", w="1u", wf="1u")
    assert client.skill == []


def test_allowlist_still_filters_resolved_names():
    client = FakeClient(TSMC28_STYLE, master="tsmcN28|nch_lvt_mac")
    with pytest.warns(UserWarning, match=r"params ignored by filter .*\['Wfg'\]"):
        applied = set_instance_params(client, "X0", wf="500n", l="30n")
    assert applied == {"l": "30n"}

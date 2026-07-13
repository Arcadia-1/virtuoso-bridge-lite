import json
import math

import pytest

from analog_design.sizing.base import SizingError
from analog_design.sizing.square_law import size_two_stage_miller
from analog_design.spec import load_design_spec
from analog_design.topology.registry import default_registry


def make_spec(tmp_path, *, ugbw="10MHz", slew="5V/us", load="5pF", vdd="3.3V"):
    data = {
        "version": 1,
        "metadata": {"name": "golden_miller"},
        "technology": {"profile": "smic180", "supply_domain": "3v3"},
        "circuit": {"class": "opamp", "topology": "two_stage_miller"},
        "interfaces": {"input_pair": "nmos"},
        "operating_conditions": {"vdd": vdd, "temperature": "27C"},
        "loads": {"output_capacitance": load},
        "metrics": [
            {"id": "gain", "kind": "hard", "analysis": "ac", "operator": ">=", "value": "60dB"},
            {"id": "ugbw", "kind": "hard", "analysis": "ac", "operator": ">=", "value": ugbw},
            {"id": "slew_rate", "kind": "hard", "analysis": "tran", "operator": ">=", "value": slew},
        ],
        "pvt": {"corners": ["tt"], "voltages": [vdd], "temperatures": ["27C"]},
        "preferences": {},
        "publication": {},
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_design_spec(path)


def test_square_law_sizing_returns_finite_engineering_seed_with_provenance(tmp_path):
    spec = make_spec(tmp_path)
    plan = default_registry().create("two_stage_miller", spec.interfaces)
    result = size_two_stage_miller(spec, plan)
    for name in ("input_gm", "tail_current", "second_stage_current", "miller_capacitance", "input_pair_width", "channel_length"):
        value = result.value(name)
        assert math.isfinite(value) and value > 0
    assert result.records["input_gm"].formula_id == "gm_from_ugbw_and_load"
    assert result.records["input_pair_width"].status == "estimate"
    assert result.records["input_pair_width"].assumptions
    assert result.confirmed_values == {}


def test_slew_requirement_increases_required_bias_current(tmp_path):
    slow = size_two_stage_miller(make_spec(tmp_path, slew="1V/us"), default_registry().create("two_stage_miller", {"input_pair": "nmos"}))
    fast = size_two_stage_miller(make_spec(tmp_path, slew="20V/us"), default_registry().create("two_stage_miller", {"input_pair": "nmos"}))
    assert fast.value("tail_current") > slow.value("tail_current")


def test_invalid_supply_is_rejected_instead_of_silently_clipped(tmp_path):
    spec = make_spec(tmp_path, vdd="0.2V")
    plan = default_registry().create("two_stage_miller", spec.interfaces)
    with pytest.raises(SizingError, match="supply"):
        size_two_stage_miller(spec, plan)

import json

import pytest

from analog_design.jsonio import StrictJsonError, load_strict_json
from analog_design.spec import SpecError, load_design_spec


def _valid_spec():
    return {
        "version": 1,
        "metadata": {"name": "golden_miller"},
        "technology": {"profile": "smic180", "supply_domain": "3v3"},
        "circuit": {"class": "opamp", "topology": "two_stage_miller"},
        "interfaces": {"input_pair": "nmos"},
        "operating_conditions": {"vdd": "3.3V", "temperature": "27C"},
        "loads": {"output_capacitance": "5pF"},
        "metrics": [
            {"id": "gain", "kind": "hard", "analysis": "ac", "operator": ">=", "value": "60dB"},
            {"id": "ugbw", "kind": "objective", "analysis": "ac", "operator": ">=", "value": "10MHz"},
            {"id": "phase_margin", "kind": "report", "analysis": "stb", "status": "unverified", "value": "60deg"},
        ],
        "pvt": {"corners": ["tt"], "voltages": ["3.3V"], "temperatures": ["27C"]},
        "preferences": {},
        "publication": {},
    }


def test_load_strict_json_rejects_nonfinite_constants(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(StrictJsonError, match="non-finite"):
        load_strict_json(path)


def test_load_design_spec_normalizes_metrics_and_conditions(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_valid_spec()), encoding="utf-8")
    spec = load_design_spec(path)
    assert spec.version == 1
    assert spec.vdd == pytest.approx(3.3)
    assert spec.output_capacitance == pytest.approx(5e-12)
    assert [metric.kind for metric in spec.metrics] == ["hard", "objective", "report"]
    assert spec.metrics[1].value == pytest.approx(10e6)
    assert spec.metrics[2].status == "unverified"


def test_metric_kind_must_be_known(tmp_path):
    data = _valid_spec()
    data["metrics"][0]["kind"] = "wish"
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SpecError, match="kind"):
        load_design_spec(path)


def test_boolean_metric_value_is_not_numeric(tmp_path):
    data = _valid_spec()
    data["metrics"][1]["value"] = True
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SpecError, match="value"):
        load_design_spec(path)


def test_ordinary_ac_cannot_claim_phase_margin(tmp_path):
    data = _valid_spec()
    data["metrics"][2].update({"analysis": "ac", "status": "verified"})
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SpecError, match="phase margin.*STB"):
        load_design_spec(path)

import json
from pathlib import Path
import sys

import pytest

OPT_ROOT = Path(__file__).resolve().parents[2] / "skills" / "smic180-analog-optimizer-v2"
sys.path.insert(0, str(OPT_ROOT))
from analog_opt.schema import load_config

from analog_design.adapters.optimizer_v2 import AdapterError, prepare_optimizer_v2_handoff
from analog_design.ir import circuit_ir_from_data
from test_circuit_ir import valid_ir_data


def evidence():
    return {
        "input_pair_width": {
            "instance": "M1",
            "property": "w",
            "unit": "um",
            "linked_instances": ["M2"],
            "sync_property": "fw",
            "sync_factor": 1.0,
            "lower": 2e-6,
            "upper": 40e-6,
        },
    }

def test_optimizer_adapter_requires_equivalence_and_cdf_evidence(tmp_path):
    ir = circuit_ir_from_data(valid_ir_data())
    with pytest.raises(AdapterError, match="equivalence"):
        prepare_optimizer_v2_handoff(ir, tmp_path, library="lib", source_cell="source", work_cell="work", result_cell="result", testbench_cell="tb", equivalence_confirmed=False, cdf_evidence=evidence(), model_includes=(("/pdk/models.scs", "tt"), ("/pdk/models.scs", "mim_tt")))
    with pytest.raises(AdapterError, match="CDF evidence"):
        prepare_optimizer_v2_handoff(ir, tmp_path, library="lib", source_cell="source", work_cell="work", result_cell="result", testbench_cell="tb", equivalence_confirmed=True, cdf_evidence={})


def test_optimizer_adapter_requires_distinct_cells(tmp_path):
    with pytest.raises(AdapterError, match="distinct"):
        prepare_optimizer_v2_handoff(circuit_ir_from_data(valid_ir_data()), tmp_path, library="lib", source_cell="source", work_cell="source", result_cell="result", testbench_cell="tb", equivalence_confirmed=True, cdf_evidence=evidence(), model_includes=(("/pdk/models.scs", "tt"), ("/pdk/models.scs", "mim_tt")))


def test_optimizer_adapter_emits_schema_valid_v2_config_and_baseline(tmp_path):
    data = valid_ir_data()
    data["measurements"] = [{"id": "gain", "analysis": "ac", "kind": "hard", "operator": ">=", "target": 60.0, "status": "requested"}]
    outputs = prepare_optimizer_v2_handoff(circuit_ir_from_data(data), tmp_path, library="lib", source_cell="source", work_cell="work", result_cell="result", testbench_cell="tb", equivalence_confirmed=True, cdf_evidence=evidence(), model_includes=(("/pdk/models.scs", "tt"), ("/pdk/models.scs", "mim_tt")))
    config = load_config(outputs.config)
    assert config.version == 2
    assert config.design.cell == "source"
    assert config.design.work_cell == "work"
    assert config.design.result_cell == "result"
    assert config.parameters[0]["target"] == "virtuoso_cdf"
    assert config.parameters[0]["instance"] == "M1"
    assert config.parameters[0]["sync_property"] == "fw"
    assert config.parameters[0]["lower"] == pytest.approx(2e-6)
    assert config.parameters[0]["linked_instances"] == ["M2"]
    baseline = json.loads(outputs.baseline.read_text(encoding="utf-8"))
    assert baseline["input_pair_width"] == pytest.approx(10e-6)
    raw = json.loads(outputs.config.read_text(encoding="utf-8"))
    assert raw["pvt"]["voltage_stimulus"] == "VDD"
    assert raw["stimuli"]["VDD"]["optimizable"] is False
    assert raw["specs"][0]["metric"] == "ac.ac_main.gain_dc_db"
    assert raw["search"] == {"method": "random", "evaluations": 20, "seed": 7}
    runtime_sim = json.loads((tmp_path / "run" / "sim_config.json").read_text(encoding="utf-8"))
    assert runtime_sim["model_includes"] == [
        {"path": "/pdk/models.scs", "section": "tt"},
        {"path": "/pdk/models.scs", "section": "mim_tt"},
    ]



def test_optimizer_adapter_maps_explicit_ir_bias_parameter_to_optimizable_stimulus(tmp_path):
    data = valid_ir_data()
    data["parameters"].append({
        "id": "tail_bias_voltage",
        "dimension": "voltage",
        "value": "0.9V",
        "bounds": {"minimum": "0.7V", "maximum": "1.2V"},
        "target": "bias",
        "linked_instances": ["M1"],
        "quantization": None,
        "provenance": {"source": "initial_sizing"},
    })
    data["measurements"] = [{"id": "gain", "analysis": "ac", "kind": "hard", "operator": ">=", "target": 60.0, "status": "requested"}]
    outputs = prepare_optimizer_v2_handoff(
        circuit_ir_from_data(data), tmp_path,
        library="lib", source_cell="source", work_cell="work", result_cell="result", testbench_cell="tb",
        equivalence_confirmed=True, cdf_evidence=evidence(),
        bias_mapping={"tail_bias_voltage": "IBIAS"},
    )
    raw = json.loads(outputs.config.read_text(encoding="utf-8"))
    bias = next(item for item in raw["parameters"] if item["name"] == "tail_bias_voltage")
    assert bias == {
        "name": "tail_bias_voltage", "target": "bias", "stimulus": "IBIAS",
        "lower": 0.7, "upper": 1.2, "dtype": "float", "scale": "linear",
    }
    assert raw["stimuli"]["IBIAS"]["optimizable"] is True
    assert raw["stimuli"]["IBIAS"]["lower"] == pytest.approx(0.7)
    assert raw["stimuli"]["IBIAS"]["upper"] == pytest.approx(1.2)
    baseline = json.loads(outputs.baseline.read_text(encoding="utf-8"))
    assert baseline["tail_bias_voltage"] == pytest.approx(0.9)
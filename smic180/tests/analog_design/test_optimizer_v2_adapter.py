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
        "input_pair_width": {"instance": "M1", "property": "w", "unit": "um", "linked_instances": ["M1", "M2"]},
    }


def test_optimizer_adapter_requires_equivalence_and_cdf_evidence(tmp_path):
    ir = circuit_ir_from_data(valid_ir_data())
    with pytest.raises(AdapterError, match="equivalence"):
        prepare_optimizer_v2_handoff(ir, tmp_path, library="lib", source_cell="source", work_cell="work", result_cell="result", testbench_cell="tb", equivalence_confirmed=False, cdf_evidence=evidence())
    with pytest.raises(AdapterError, match="CDF evidence"):
        prepare_optimizer_v2_handoff(ir, tmp_path, library="lib", source_cell="source", work_cell="work", result_cell="result", testbench_cell="tb", equivalence_confirmed=True, cdf_evidence={})


def test_optimizer_adapter_requires_distinct_cells(tmp_path):
    with pytest.raises(AdapterError, match="distinct"):
        prepare_optimizer_v2_handoff(circuit_ir_from_data(valid_ir_data()), tmp_path, library="lib", source_cell="source", work_cell="source", result_cell="result", testbench_cell="tb", equivalence_confirmed=True, cdf_evidence=evidence())


def test_optimizer_adapter_emits_schema_valid_v2_config_and_baseline(tmp_path):
    data = valid_ir_data()
    data["measurements"] = [{"id": "gain", "analysis": "ac", "kind": "hard", "operator": ">=", "target": 60.0, "status": "requested"}]
    outputs = prepare_optimizer_v2_handoff(circuit_ir_from_data(data), tmp_path, library="lib", source_cell="source", work_cell="work", result_cell="result", testbench_cell="tb", equivalence_confirmed=True, cdf_evidence=evidence())
    config = load_config(outputs.config)
    assert config.version == 2
    assert config.design.cell == "source"
    assert config.design.work_cell == "work"
    assert config.design.result_cell == "result"
    assert config.parameters[0]["target"] == "virtuoso_cdf"
    assert config.parameters[0]["instance"] == "M1"
    baseline = json.loads(outputs.baseline.read_text(encoding="utf-8"))
    assert baseline["input_pair_width"] == pytest.approx(10e-6)
    raw = json.loads(outputs.config.read_text(encoding="utf-8"))
    assert raw["pvt"]["voltage_stimulus"] == "VDD"
    assert raw["stimuli"]["VDD"]["optimizable"] is False
    assert raw["specs"]


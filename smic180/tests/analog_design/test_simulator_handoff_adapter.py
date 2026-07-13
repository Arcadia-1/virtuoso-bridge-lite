import json
from pathlib import Path
import sys

import pytest

SIM_ROOT = Path(__file__).resolve().parents[2] / "skills" / "smic180-simulator"
sys.path.insert(0, str(SIM_ROOT))
from sim_io.pin_types import load_pin_classifications
from sim_io.sim.config import load_sim_config

from analog_design.adapters.simulator import AdapterError, prepare_simulator_handoff
from analog_design.ir import circuit_ir_from_data
from test_circuit_ir import valid_ir_data


def analog_ir_data():
    data = valid_ir_data()
    data["ports"].append({"id": "IBIAS", "direction": "input", "kind": "bias"})
    data["nets"].append({"id": "IBIAS", "critical": True})
    data["biases"] = [{"id": "tail_bias", "net": "IBIAS", "value": "0.9V"}]
    data["constraints"] = [{"id": "output_load", "kind": "capacitance", "net": "VOUT", "value": "5pF"}]
    return data


def test_simulator_adapter_requires_equivalence_gate(tmp_path):
    with pytest.raises(AdapterError, match="equivalence"):
        prepare_simulator_handoff(
            circuit_ir_from_data(analog_ir_data()),
            tmp_path,
            library="lib",
            cell="amp",
            equivalence_confirmed=False,
            model_includes=(("/pdk/models.scs", "tt"), ("/pdk/models.scs", "mim_tt")),
        )


def test_simulator_adapter_emits_existing_loader_validated_analog_intent(tmp_path):
    outputs = prepare_simulator_handoff(
        circuit_ir_from_data(analog_ir_data()),
        tmp_path,
        library="lib",
        cell="amp",
        equivalence_confirmed=True,
        model_includes=(("/pdk/models.scs", "tt"), ("/pdk/models.scs", "mim_tt")),
    )
    pins_raw = json.loads(outputs.pin_classifications.read_text(encoding="utf-8"))
    config_raw = json.loads(outputs.sim_config.read_text(encoding="utf-8"))
    review = json.loads(outputs.review_required.read_text(encoding="utf-8"))
    loaded_pins = load_pin_classifications(outputs.pin_classifications)
    loaded_config = load_sim_config(outputs.sim_config)
    by_name = {item.name: item for item in loaded_pins.pins}

    assert pins_raw["lib"] == "lib"
    assert pins_raw["cell"] == "amp"
    assert set(by_name) == {"VDD", "VSS", "VINP", "VINN", "VOUT", "IBIAS"}
    assert by_name["VDD"].device_class == "analog_power"
    assert by_name["VDD"].local_pvss == "VSS"
    assert by_name["VDD"].stimulus == "vdc"
    assert by_name["VDD"].stimulus_params == {"dc": "3.3"}
    assert by_name["VSS"].device_class == "analog_ground"
    assert by_name["VINP"].stimulus_params == {"dc": "1.65", "acm": "1", "acp": "0"}
    assert by_name["VINN"].stimulus_params == {"dc": "1.65"}
    assert by_name["IBIAS"].device_class == "analog_input"
    assert by_name["IBIAS"].stimulus_params == {"dc": "0.9"}
    assert by_name["VOUT"].load_params == {"c": "5p"}
    assert {analysis.name for analysis in loaded_config.analyses} >= {"dc", "ac", "tran"}
    assert config_raw["model_includes"] == [
        {"path": "/pdk/models.scs", "section": "tt"},
        {"path": "/pdk/models.scs", "section": "mim_tt"},
    ]
    assert review["required"] is True
    assert review["status"] == "reviewed"
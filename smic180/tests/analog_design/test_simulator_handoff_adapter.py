import json

import pytest

from analog_design.adapters.simulator import AdapterError, prepare_simulator_handoff
from analog_design.ir import circuit_ir_from_data
from test_circuit_ir import valid_ir_data


def test_simulator_adapter_requires_equivalence_gate(tmp_path):
    with pytest.raises(AdapterError, match="equivalence"):
        prepare_simulator_handoff(circuit_ir_from_data(valid_ir_data()), tmp_path, equivalence_confirmed=False)


def test_simulator_adapter_emits_reviewed_pin_and_deck_intent(tmp_path):
    outputs = prepare_simulator_handoff(circuit_ir_from_data(valid_ir_data()), tmp_path, equivalence_confirmed=True)
    pins = json.loads(outputs.pin_classifications.read_text(encoding="utf-8"))
    config = json.loads(outputs.sim_config.read_text(encoding="utf-8"))
    review = json.loads(outputs.review_required.read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in pins}
    assert by_name["VDD"]["device_class"] == "analog_power"
    assert by_name["VSS"]["device_class"] == "analog_ground"
    assert by_name["VINP"]["device_class"] == "analog_input"
    assert by_name["VOUT"]["device_class"] == "analog_output"
    assert config["model_includes"]
    assert {item["type"] for item in config["analyses"]} >= {"dc", "ac"}
    assert review["required"] is True
    assert "polarity" in " ".join(review["checks"])

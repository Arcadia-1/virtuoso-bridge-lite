import copy

import pytest

from test_circuit_ir import valid_ir_data
from analog_design.ir import circuit_ir_from_data
from analog_design.validation import ValidationError, validate_circuit_ir


def test_critical_port_must_be_connected_to_an_instance():
    data = valid_ir_data()
    data["instances"] = [item for item in data["instances"] if item["id"] != "RLOAD"]
    ir = circuit_ir_from_data(data)
    with pytest.raises(ValidationError, match="critical port VDD is floating"):
        validate_circuit_ir(ir)


def test_matching_group_instances_must_reference_group_and_parameter():
    data = valid_ir_data()
    data["instances"][1]["matching_groups"] = []
    ir = circuit_ir_from_data(data)
    with pytest.raises(ValidationError, match="M2.*input_pair"):
        validate_circuit_ir(ir)


def test_linked_parameter_instances_must_exist():
    data = valid_ir_data()
    data["parameters"][0]["linked_instances"].append("MISSING")
    ir = circuit_ir_from_data(data)
    with pytest.raises(ValidationError, match="unknown instance MISSING"):
        validate_circuit_ir(ir)


def test_matching_devices_must_share_linked_parameter():
    data = valid_ir_data()
    data["parameters"][0]["linked_instances"] = ["M1"]
    ir = circuit_ir_from_data(data)
    with pytest.raises(ValidationError, match="matching group input_pair.*linked"):
        validate_circuit_ir(ir)


def test_valid_ir_passes_electrical_validation():
    validate_circuit_ir(circuit_ir_from_data(valid_ir_data()))

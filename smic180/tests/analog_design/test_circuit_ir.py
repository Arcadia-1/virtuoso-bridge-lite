import copy
import json

import pytest

from analog_design.ir import IrError, canonical_ir_digest, load_circuit_ir


def valid_ir_data():
    return {
        "version": 1,
        "metadata": {"name": "miller_seed"},
        "technology": {"profile": "smic180", "profile_state": "unconfirmed"},
        "circuit": {"class": "opamp", "topology": "two_stage_miller"},
        "ports": [
            {"id": "VDD", "direction": "input", "kind": "power"},
            {"id": "VSS", "direction": "input", "kind": "ground"},
            {"id": "VINP", "direction": "input", "kind": "signal"},
            {"id": "VINN", "direction": "input", "kind": "signal"},
            {"id": "VOUT", "direction": "output", "kind": "signal"},
        ],
        "nets": [
            {"id": name, "critical": name in {"VDD", "VSS", "VINP", "VINN", "VOUT"}}
            for name in ["VDD", "VSS", "VINP", "VINN", "VOUT", "NTAIL", "N1"]
        ],
        "instances": [
            {
                "id": "M1",
                "role": "input_pair_positive",
                "device_class": "mos.nmos",
                "master_ref": "smic180.core_nmos",
                "terminals": {"D": "N1", "G": "VINP", "S": "NTAIL", "B": "VSS"},
                "logical_parameters": {"width": "10um", "length": "1um"},
                "physical_parameters": {},
                "cdf_expectations": {},
                "optimization_refs": ["input_pair_width"],
                "matching_groups": ["input_pair"],
                "rationale": ["matched differential input"],
            },
            {
                "id": "M2",
                "role": "input_pair_negative",
                "device_class": "mos.nmos",
                "master_ref": "smic180.core_nmos",
                "terminals": {"D": "VOUT", "G": "VINN", "S": "NTAIL", "B": "VSS"},
                "logical_parameters": {"width": "10um", "length": "1um"},
                "physical_parameters": {},
                "cdf_expectations": {},
                "optimization_refs": ["input_pair_width"],
                "matching_groups": ["input_pair"],
                "rationale": ["matched differential input"],
            },
            {
                "id": "IBIAS",
                "role": "tail_bias",
                "device_class": "source.current",
                "master_ref": "analog.current_source",
                "terminals": {"P": "NTAIL", "N": "VSS"},
                "logical_parameters": {"dc": "20uA"},
                "physical_parameters": {},
                "cdf_expectations": {},
                "optimization_refs": [],
                "matching_groups": [],
                "rationale": ["tail current"],
            },
            {
                "id": "RLOAD",
                "role": "output_load",
                "device_class": "passive.resistor",
                "master_ref": "analog.resistor",
                "terminals": {"P": "VDD", "N": "VOUT"},
                "logical_parameters": {"resistance": 100000.0},
                "physical_parameters": {},
                "cdf_expectations": {},
                "optimization_refs": [],
                "matching_groups": [],
                "rationale": ["test connectivity"],
            },
        ],
        "parameters": [
            {
                "id": "input_pair_width",
                "dimension": "length",
                "value": "10um",
                "bounds": {"minimum": "2um", "maximum": "40um"},
                "target": "device",
                "linked_instances": ["M1", "M2"],
                "quantization": None,
                "provenance": {"source": "initial_sizing"},
            }
        ],
        "matching_groups": [{"id": "input_pair", "instances": ["M1", "M2"], "parameters": ["input_pair_width"]}],
        "supplies": [{"id": "main_supply", "positive": "VDD", "negative": "VSS", "value": "3.3V"}],
        "biases": [{"id": "tail_current", "instance": "IBIAS", "value": "20uA"}],
        "analyses": [{"id": "op", "type": "dc_op"}],
        "measurements": [{"id": "supply_current", "analysis": "op"}],
        "constraints": [],
        "optimization": {"enabled": True},
        "provenance": {"design_spec_digest": "abc"},
    }


def write_ir(tmp_path, data):
    path = tmp_path / "circuit_ir.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_valid_circuit_ir_and_digest_is_canonical(tmp_path):
    data = valid_ir_data()
    ir = load_circuit_ir(write_ir(tmp_path, data))
    assert ir.version == 1
    assert ir.instance("M1").terminals["G"] == "VINP"
    first = canonical_ir_digest(data)
    reordered = {key: data[key] for key in reversed(list(data))}
    assert canonical_ir_digest(reordered) == first


def test_missing_required_top_level_field_is_rejected(tmp_path):
    data = valid_ir_data()
    del data["provenance"]
    with pytest.raises(IrError, match="provenance"):
        load_circuit_ir(write_ir(tmp_path, data))


def test_duplicate_instance_id_is_rejected(tmp_path):
    data = valid_ir_data()
    data["instances"].append(copy.deepcopy(data["instances"][0]))
    with pytest.raises(IrError, match="duplicate instance"):
        load_circuit_ir(write_ir(tmp_path, data))


def test_unknown_terminal_net_is_rejected(tmp_path):
    data = valid_ir_data()
    data["instances"][0]["terminals"]["D"] = "MISSING"
    with pytest.raises(IrError, match="unknown net MISSING"):
        load_circuit_ir(write_ir(tmp_path, data))


def test_instance_requires_nonempty_terminal_map(tmp_path):
    data = valid_ir_data()
    data["instances"][0]["terminals"] = {}
    with pytest.raises(IrError, match="terminals"):
        load_circuit_ir(write_ir(tmp_path, data))


def test_parameter_bounds_must_be_ordered_and_contain_value(tmp_path):
    data = valid_ir_data()
    data["parameters"][0]["bounds"] = {"minimum": "20um", "maximum": "5um"}
    with pytest.raises(IrError, match="bounds"):
        load_circuit_ir(write_ir(tmp_path, data))

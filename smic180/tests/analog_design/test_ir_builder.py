import json

from analog_design.builder import build_circuit_ir
from analog_design.sizing.square_law import size_two_stage_miller
from analog_design.spec import load_design_spec
from analog_design.technology.smic180 import create_offline_smic180_profile
from analog_design.topology.registry import default_registry
from analog_design.validation import validate_circuit_ir


def load_spec(tmp_path):
    data = {
        "version": 1,
        "metadata": {"name": "golden_miller"},
        "technology": {"profile": "smic180", "supply_domain": "3v3"},
        "circuit": {"class": "opamp", "topology": "two_stage_miller"},
        "interfaces": {"input_pair": "nmos"},
        "operating_conditions": {"vdd": "3.3V", "temperature": "27C"},
        "loads": {"output_capacitance": "5pF"},
        "metrics": [
            {"id": "gain", "kind": "hard", "analysis": "ac", "operator": ">=", "value": "60dB"},
            {"id": "ugbw", "kind": "hard", "analysis": "ac", "operator": ">=", "value": "10MHz"},
            {"id": "slew_rate", "kind": "hard", "analysis": "tran", "operator": ">=", "value": "5V/us"},
        ],
        "pvt": {"corners": ["tt"], "voltages": ["3.3V"], "temperatures": ["27C"]},
        "preferences": {},
        "publication": {},
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_design_spec(path)


def test_builder_combines_spec_topology_sizing_and_profile_into_valid_ir(tmp_path):
    spec = load_spec(tmp_path)
    topology = default_registry().create("two_stage_miller", spec.interfaces)
    sizing = size_two_stage_miller(spec, topology)
    ir = build_circuit_ir(spec, topology, sizing, create_offline_smic180_profile())
    validate_circuit_ir(ir)
    assert ir.circuit["topology"] == "two_stage_miller"
    assert ir.instance("M_IN_P").master_ref == "smic180.core_nmos"
    assert ir.instance("C_MILLER").logical_parameters["capacitance"] == sizing.value("miller_capacitance")
    assert {item.id for item in ir.parameters} >= {"input_pair_width", "channel_length", "tail_bias_voltage"}
    assert ir.provenance["sizing_status"] == "estimate"

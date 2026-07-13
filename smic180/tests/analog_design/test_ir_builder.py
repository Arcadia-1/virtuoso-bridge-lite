import json

import pytest

from analog_design.builder import build_circuit_ir
from analog_design.sizing.square_law import size_two_stage_miller
from analog_design.spec import load_design_spec
from analog_design.technology.base import DeviceAdapter, TechnologyProfile
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


def test_builder_sizes_real_second_stage_bias_mos(tmp_path):
    spec = load_spec(tmp_path)
    topology = default_registry().create("two_stage_miller", spec.interfaces)
    sizing = size_two_stage_miller(spec, topology)
    ir = build_circuit_ir(spec, topology, sizing, create_offline_smic180_profile())
    bias = ir.instance("M_SECOND_BIAS")
    assert bias.master_ref == "smic180.core_nmos"
    assert bias.logical_parameters["width"] > 0
    assert "second_stage_bias_width" in {item.id for item in ir.parameters}

def confirmed_profile():
    common_evidence = {"master": "master.json", "terminals": "terminals.json", "cdf": "roundtrip.json"}
    nmos = DeviceAdapter(
        "smic180.core_nmos", "mos.nmos", "smic18ee", "n33e2r", "symbol", ("D", "G", "B", "S"),
        {"width": "w", "finger_width": "fw", "length": "l", "fingers": "fingers", "multiplier": "m"},
        {"width": "length", "finger_width": "length", "length": "length", "fingers": "integer", "multiplier": "integer"},
        common_evidence,
        netlist_model="n33e2r", netlist_terminals=("D", "G", "S", "B"),
        limits={"minimum_length": 600e-9, "minimum_finger_width": 600e-9},
    )
    pmos = DeviceAdapter(
        "smic180.core_pmos", "mos.pmos", "smic18ee", "p33e2r", "symbol", ("B", "D", "G", "S"),
        {"width": "w", "finger_width": "fw", "length": "l", "fingers": "fingers", "multiplier": "m"},
        {"width": "length", "finger_width": "length", "length": "length", "fingers": "integer", "multiplier": "integer"},
        common_evidence,
        netlist_model="p33e2r", netlist_terminals=("D", "G", "S", "B"),
        limits={"minimum_length": 800e-9, "minimum_finger_width": 600e-9},
    )
    cap = DeviceAdapter(
        "smic180.miller_capacitor", "passive.capacitor", "smic18ee", "mime2r", "symbol", ("PLUS", "MINUS"),
        {"width": "w", "length": "l", "multiplier": "m", "capacitance": "c"},
        {"width": "length", "length": "length", "multiplier": "integer", "capacitance": "capacitance"},
        common_evidence,
        terminal_map={"P": "PLUS", "N": "MINUS"},
        netlist_model="mime2r", netlist_terminals=("PLUS", "MINUS"),
        limits={"maximum_width": 30e-6, "maximum_length": 30e-6, "area_cap_density": 971e-6},
    )
    return TechnologyProfile(
        "smic180", "confirmed",
        {item.master_ref: item for item in (nmos, pmos, cap)},
        {"pdk_root": "/pdk", "cds_lib": "/pdk/cds.lib"},
        model_sections={"tt": ("tt", "mim_tt")},
    )


def test_confirmed_profile_physicalizes_mos_and_mim_into_ir(tmp_path):
    spec = load_spec(tmp_path)
    topology = default_registry().create("two_stage_miller", spec.interfaces)
    ir = build_circuit_ir(spec, topology, size_two_stage_miller(spec, topology), confirmed_profile())
    mos = ir.instance("M_IN_P")
    assert mos.physical_parameters["total_width"] == mos.logical_parameters["width"]
    assert mos.physical_parameters["finger_width"] == mos.logical_parameters["width"]
    assert mos.cdf_expectations["w"] == mos.logical_parameters["width"]
    assert mos.cdf_expectations["fw"] == mos.logical_parameters["width"]
    cap = ir.instance("C_MILLER")
    effective = 971e-6 * cap.physical_parameters["width"] * cap.physical_parameters["length"] * cap.physical_parameters["multiplier"]
    assert effective == pytest.approx(cap.logical_parameters["capacitance"], rel=1e-9)
    assert cap.physical_parameters["width"] <= 30e-6
    assert cap.physical_parameters["length"] <= 30e-6
    assert cap.cdf_expectations["m"] == cap.physical_parameters["multiplier"]

def test_builder_records_output_load_as_structured_constraint(tmp_path):
    spec = load_spec(tmp_path)
    topology = default_registry().create("two_stage_miller", spec.interfaces)
    ir = build_circuit_ir(spec, topology, size_two_stage_miller(spec, topology), create_offline_smic180_profile())
    load = next(item for item in ir.constraints if item["id"] == "output_load")
    assert load == {"id": "output_load", "kind": "capacitance", "net": "VOUT", "value": spec.output_capacitance}
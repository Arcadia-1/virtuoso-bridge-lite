"""Build version-1 Circuit IR from specification, topology, and sizing."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .ir import CircuitIr, circuit_ir_from_data
from .sizing.base import SizingResult
from .spec import DesignSpec
from .technology.base import TechnologyProfile
from .technology.smic180 import physicalize_smic180_instance
from .topology.base import TopologyPlan


_MASTER_REFS = {
    "mos.nmos": "smic180.core_nmos",
    "mos.pmos": "smic180.core_pmos",
    "source.current": "analog.current_source",
    "passive.capacitor": "smic180.miller_capacitor",
    "passive.resistor": "smic180.nulling_resistor",
}


def _parameter(identity: str, dimension: str, value: float, minimum: float, maximum: float, linked: list[str], target: str = "device") -> dict[str, Any]:
    return {
        "id": identity,
        "dimension": dimension,
        "value": value,
        "bounds": {"minimum": minimum, "maximum": maximum},
        "target": target,
        "linked_instances": linked,
        "quantization": None,
        "provenance": {"source": "initial_sizing", "status": "estimate"},
    }


def _logical_parameters(role: str, sizing: SizingResult) -> dict[str, Any]:
    length = sizing.value("channel_length")
    input_width = sizing.value("input_pair_width")
    if role.startswith("input_pair"):
        return {"width": input_width, "length": length, "fingers": 1, "multiplier": 1}
    if role in {"mirror_diode", "mirror_output"}:
        return {"width": input_width, "length": length, "fingers": 1, "multiplier": 1}
    if role == "tail_source":
        return {"width": max(input_width * 0.5, 1e-6), "length": length, "fingers": 1, "multiplier": 1}
    if role == "second_stage":
        return {"width": max(input_width * 2.0, 1e-6), "length": length, "fingers": 1, "multiplier": 1}
    if role == "second_stage_bias":
        return {"width": max(input_width, 1e-6), "length": length, "fingers": 1, "multiplier": 1}
    if role == "miller_compensation":
        return {"capacitance": sizing.value("miller_capacitance")}
    if role == "nulling_resistor":
        return {"resistance": 0.0}
    return {}


def build_circuit_ir(spec: DesignSpec, topology: TopologyPlan, sizing: SizingResult, technology: TechnologyProfile) -> CircuitIr:
    enabled = [item for item in topology.instances if item.enabled]
    instances: list[dict[str, Any]] = []
    for item in enabled:
        master_ref = _MASTER_REFS[item.device_class]
        adapter = technology.resolve(master_ref)
        logical_parameters = _logical_parameters(item.role, sizing)
        if technology.state == "confirmed":
            physical_parameters, cdf_expectations = physicalize_smic180_instance(adapter, logical_parameters)
        else:
            physical_parameters, cdf_expectations = {}, {}
        groups = [name for name, members in topology.matching_groups.items() if item.id in members]
        optimization_refs: list[str] = []
        if item.role.startswith("input_pair"):
            optimization_refs = ["input_pair_width", "channel_length"]
        elif item.role in {"mirror_diode", "mirror_output"}:
            optimization_refs = ["active_load_width", "channel_length"]
        elif item.role == "tail_source":
            optimization_refs = ["tail_device_width", "channel_length", "tail_bias_voltage"]
        elif item.role == "second_stage":
            optimization_refs = ["second_stage_width", "channel_length"]
        elif item.role == "second_stage_bias":
            optimization_refs = ["second_stage_bias_width", "channel_length"]
        instances.append({
            "id": item.id,
            "role": item.role,
            "device_class": item.device_class,
            "master_ref": master_ref,
            "terminals": dict(item.terminals),
            "logical_parameters": logical_parameters,
            "physical_parameters": physical_parameters,
            "cdf_expectations": cdf_expectations,
            "optimization_refs": optimization_refs,
            "matching_groups": groups,
            "rationale": [f"role from {topology.id} topology plan"],
        })
    width = sizing.value("input_pair_width")
    length = sizing.value("channel_length")
    parameters = [
        _parameter("input_pair_width", "length", width, max(width * 0.25, 1e-7), width * 4.0, ["M_IN_P", "M_IN_N"]),
        _parameter("active_load_width", "length", width, max(width * 0.25, 1e-7), width * 4.0, ["M_LOAD_DIODE", "M_LOAD_OUT"]),
        _parameter("tail_device_width", "length", max(width * 0.5, 1e-6), 1e-7, max(width * 4.0, 4e-6), ["M_TAIL"]),
        _parameter("second_stage_width", "length", max(width * 2.0, 1e-6), 1e-7, max(width * 8.0, 8e-6), ["M_SECOND"]),
        _parameter("second_stage_bias_width", "length", max(width, 1e-6), 1e-7, max(width * 4.0, 4e-6), ["M_SECOND_BIAS"]),
        _parameter("channel_length", "length", length, max(length * 0.5, 1e-7), length * 4.0, ["M_IN_P", "M_IN_N", "M_LOAD_DIODE", "M_LOAD_OUT", "M_TAIL", "M_SECOND", "M_SECOND_BIAS"]),
        _parameter("tail_bias_voltage", "voltage", min(spec.vdd * 0.3, 0.9), 0.1, max(0.2, spec.vdd * 0.6), ["M_TAIL"], "bias"),
    ]
    port_kind = {"VDD": "power", "VSS": "ground", "VINP": "signal", "VINN": "signal", "VOUT": "signal", "IBIAS": "bias"}
    direction = {"VOUT": "output"}
    data = {
        "version": 1,
        "metadata": dict(spec.metadata),
        "technology": {"profile": technology.name, "profile_state": technology.state},
        "circuit": {"class": spec.circuit["class"], "topology": topology.id},
        "ports": [{"id": item, "direction": direction.get(item, "input"), "kind": port_kind[item]} for item in topology.ports],
        "nets": [{"id": item, "critical": item in topology.ports} for item in topology.nets],
        "instances": instances,
        "parameters": parameters,
        "matching_groups": [
            {"id": "input_pair", "instances": list(topology.matching_groups["input_pair"]), "parameters": ["input_pair_width", "channel_length"]},
            {"id": "active_load", "instances": list(topology.matching_groups["active_load"]), "parameters": ["active_load_width", "channel_length"]},
        ],
        "supplies": [{"id": "main_supply", "positive": "VDD", "negative": "VSS", "value": spec.vdd}],
        "biases": [{"id": "tail_bias", "net": "IBIAS", "value": min(spec.vdd * 0.3, 0.9)}],
        "analyses": [
            {"id": "op", "type": "dc_op"},
            {"id": "ac", "type": "ac", "start": 1.0, "stop": 1e9, "points_per_decade": 20},
            {"id": "tran", "type": "tran", "stop": 20e-6},
        ],
        "measurements": [
            {"id": metric.id, "analysis": metric.analysis, "kind": metric.kind, "operator": metric.operator, "target": metric.value, "status": metric.status}
            for metric in spec.metrics
        ],
        "constraints": [{"id": "output_load", "kind": "capacitance", "net": "VOUT", "value": spec.output_capacitance}],
        "optimization": {"enabled": True, "parameters": [item["id"] for item in parameters]},
        "provenance": {"sizing_status": "estimate", "topology_basis": list(topology.selection_basis), "technology_state": technology.state},
    }
    return circuit_ir_from_data(data)

"""Prepare strict schema-valid handoff to SMIC180 Analog Optimizer V2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..artifacts import ArtifactStore
from ..ir import CircuitIr
from ..units import parse_quantity
from .simulator import AdapterError


@dataclass(frozen=True)
class OptimizerV2Handoff:
    config: Path
    baseline: Path
    evidence: Path


def _safe_identifier(value: str, label: str) -> str:
    if not value or not value.replace("_", "a").isalnum():
        raise AdapterError(f"{label} must be a safe identifier")
    return value


def prepare_optimizer_v2_handoff(
    ir: CircuitIr,
    output_dir: str | Path,
    *,
    library: str,
    source_cell: str,
    work_cell: str,
    result_cell: str,
    testbench_cell: str,
    equivalence_confirmed: bool,
    cdf_evidence: Mapping[str, Mapping[str, Any]],
    model_includes: tuple[tuple[str, str], ...] = (),
    bias_mapping: Mapping[str, str] | None = None,
) -> OptimizerV2Handoff:
    if not equivalence_confirmed:
        raise AdapterError("Optimizer V2 handoff requires confirmed equivalence")
    cells = [_safe_identifier(source_cell, "source cell"), _safe_identifier(work_cell, "work cell"), _safe_identifier(result_cell, "result cell")]
    if len(set(cells)) != 3:
        raise AdapterError("source, work, and result cells must be distinct")
    _safe_identifier(library, "library")
    _safe_identifier(testbench_cell, "testbench cell")
    parameters = []
    baseline: dict[str, float | int] = {}
    instance_ids = {item.id for item in ir.instances}
    biases = dict(bias_mapping or {})
    for parameter in ir.parameters:
        if parameter.target == "bias":
            stimulus = biases.get(parameter.id)
            if stimulus is None:
                raise AdapterError(f"bias mapping is missing for {parameter.id}")
            parameters.append({
                "name": parameter.id, "target": "bias", "stimulus": stimulus,
                "lower": parameter.minimum, "upper": parameter.maximum,
                "dtype": "float", "scale": "linear",
            })
            baseline[parameter.id] = parameter.value
            continue
        if parameter.id not in cdf_evidence:
            continue
        proof = cdf_evidence[parameter.id]
        required = {"instance", "property", "unit", "linked_instances"}
        if not required.issubset(proof):
            raise AdapterError(f"CDF evidence is incomplete for {parameter.id}")
        instance = str(proof["instance"])
        linked = list(proof["linked_instances"])
        if instance not in instance_ids or not set(linked).issubset(instance_ids):
            raise AdapterError(f"CDF evidence references unknown instance for {parameter.id}")
        lower = float(proof.get("lower", parameter.minimum))
        upper = float(proof.get("upper", parameter.maximum))
        if lower >= upper:
            raise AdapterError(f"CDF evidence has invalid legal bounds for {parameter.id}")
        item = {
            "name": parameter.id,
            "target": "virtuoso_cdf",
            "instance": instance,
            "property": str(proof["property"]),
            "unit": str(proof["unit"]),
            "lower": lower,
            "upper": upper,
            "dtype": str(proof.get("dtype", "float")),
            "scale": str(proof.get("scale", "linear")),
        }
        if linked:
            item["linked_instances"] = linked
        if proof.get("sync_property") is not None:
            item["sync_property"] = str(proof["sync_property"])
            item["sync_factor"] = float(proof.get("sync_factor", 1.0))
        parameters.append(item)
        baseline[parameter.id] = float(proof.get("baseline", parameter.value))
    if not parameters:
        raise AdapterError("CDF evidence does not map any optimizable IR parameter")
    vdd = parse_quantity(ir.supplies[0]["value"], "voltage")
    specs = []
    for measurement in ir.measurements:
        if measurement.get("kind") != "hard" or measurement.get("operator") is None:
            continue
        metric = {"gain": "ac.ac_main.gain_dc_db", "ugbw": "ac.ac_main.unity_gain_hz"}.get(measurement["id"])
        if metric is None:
            continue
        specs.append({"metric": metric, "op": measurement["operator"], "value": measurement["target"], "hard": True})
    if not specs:
        raise AdapterError("Optimizer V2 handoff requires at least one hard specification")
    bias_parameter = next((item for item in parameters if item["target"] == "bias" and item["stimulus"] == "IBIAS"), None)
    ibias_stimulus = {"kind": "voltage", "value": 0.9, "source_instance": "SRC_IBIAS", "optimizable": False}
    if bias_parameter is not None:
        ibias_stimulus.update({"optimizable": True, "lower": bias_parameter["lower"], "upper": bias_parameter["upper"]})
    config = {
        "version": 2,
        "design": {"library": library, "cell": source_cell, "work_cell": work_cell, "result_cell": result_cell, "testbench_cell": testbench_cell, "dut_instance": "DUT"},
        "stimuli": {
            "VDD": {"kind": "voltage", "value": vdd, "source_instance": "SRC_VDD", "optimizable": False},
            "VINP": {"kind": "voltage", "dc": vdd / 2.0, "ac": 1.0, "source_instance": "SRC_VINP", "optimizable": False},
            "VINN": {"kind": "voltage", "dc": vdd / 2.0, "ac": 0.0, "source_instance": "SRC_VINN", "optimizable": False},
            "IBIAS": ibias_stimulus,
            "VSS": {"kind": "voltage", "value": 0.0, "source_instance": "PVSS_VSS", "optimizable": False},
        },
        "parameters": parameters,
        "analyses": [
            {"name": "op", "type": "dc_op", "instances": [item.id for item in ir.instances if item.device_class.startswith("mos.")], "nodes": ["VOUT"], "source_currents": {"supply": "SRC_VDD:p"}},
            {"name": "ac_main", "type": "ac", "start": 1.0, "stop": 1e9, "points_per_decade": 20, "output": "VOUT", "input": "VINP"},
        ],
        "metrics": [{"name": "gain", "analysis": "ac_main"}, {"name": "ugbw", "analysis": "ac_main"}],
        "specs": specs,
        "search": {"method": "random", "evaluations": 20, "seed": 7},
        "pvt": {"corners": ["TT"], "voltages": [vdd], "temperatures_c": [27.0], "voltage_stimulus": "VDD"},
        "outputs": {"run_dir": str(Path(output_dir).resolve() / "run")},
    }
    output = Path(output_dir)
    store = ArtifactStore(output)
    config_path = store.write_json(output / "analog_opt_v2.json", config)
    baseline_path = store.write_json(output / "baseline_candidate.json", baseline)
    evidence_path = store.write_json(output / "cdf_mapping_evidence.json", cdf_evidence)
    runtime_dir = output / "run"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    store.write_json(runtime_dir / "sim_config.json", {
        "analyses": [],
        "model_includes": [{"path": path, "section": section} for path, section in model_includes],
        "save_default": "allpub",
        "pin_measurements": {},
    })
    return OptimizerV2Handoff(config_path, baseline_path, evidence_path)




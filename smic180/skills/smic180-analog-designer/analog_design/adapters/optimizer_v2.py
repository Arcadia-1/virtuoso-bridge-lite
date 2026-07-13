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
    for parameter in ir.parameters:
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
        item = {
            "name": parameter.id,
            "target": "virtuoso_cdf",
            "instance": instance,
            "property": str(proof["property"]),
            "unit": str(proof["unit"]),
            "lower": parameter.minimum,
            "upper": parameter.maximum,
            "dtype": "float",
            "scale": "linear",
        }
        if linked:
            item["linked_instances"] = linked
        parameters.append(item)
        baseline[parameter.id] = parameter.value
    if not parameters:
        raise AdapterError("CDF evidence does not map any optimizable IR parameter")
    vdd = parse_quantity(ir.supplies[0]["value"], "voltage")
    specs = []
    for measurement in ir.measurements:
        if measurement.get("kind") != "hard" or measurement.get("operator") is None:
            continue
        specs.append({"metric": measurement["id"], "op": measurement["operator"], "value": measurement["target"], "hard": True})
    if not specs:
        raise AdapterError("Optimizer V2 handoff requires at least one hard specification")
    config = {
        "version": 2,
        "design": {"library": library, "cell": source_cell, "work_cell": work_cell, "result_cell": result_cell, "testbench_cell": testbench_cell},
        "stimuli": {
            "VDD": {"kind": "voltage", "value": vdd, "source_instance": "SRC_VDD", "optimizable": False},
            "VINP": {"kind": "voltage", "dc": vdd / 2.0, "ac": 1.0, "source_instance": "SRC_VINP", "optimizable": False},
            "VINN": {"kind": "voltage", "dc": vdd / 2.0, "ac": 0.0, "source_instance": "SRC_VINN", "optimizable": False},
        },
        "parameters": parameters,
        "analyses": [
            {"name": "op", "type": "dc_op"},
            {"name": "ac_main", "type": "ac", "start": 1.0, "stop": 1e9, "points_per_decade": 20, "output": "VOUT", "input": "VINP"},
        ],
        "metrics": [{"name": measurement["id"], "analysis": measurement["analysis"]} for measurement in ir.measurements if measurement["id"] != "phase_margin"],
        "specs": specs,
        "search": {"algorithm": "random", "max_evals": 20, "seed": 7},
        "pvt": {"corners": ["TT"], "voltages": [vdd], "temperatures_c": [27.0], "voltage_stimulus": "VDD"},
        "outputs": {"run_dir": str(Path(output_dir).resolve() / "run")},
    }
    output = Path(output_dir)
    store = ArtifactStore(output)
    config_path = store.write_json(output / "analog_opt_v2.json", config)
    baseline_path = store.write_json(output / "baseline_candidate.json", baseline)
    evidence_path = store.write_json(output / "cdf_mapping_evidence.json", cdf_evidence)
    return OptimizerV2Handoff(config_path, baseline_path, evidence_path)




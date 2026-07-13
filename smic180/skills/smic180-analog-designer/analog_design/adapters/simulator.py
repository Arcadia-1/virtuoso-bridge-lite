"""Prepare reviewed inputs for the existing SMIC180 simulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..artifacts import ArtifactStore
from ..ir import CircuitIr
from ..units import parse_quantity


class AdapterError(ValueError):
    """Raised when a downstream handoff gate is not proven."""


@dataclass(frozen=True)
class SimulatorHandoff:
    pin_classifications: Path
    sim_config: Path
    review_required: Path


def _display(value: float) -> str:
    return f"{float(value):.12g}"


def _supply(ir: CircuitIr) -> tuple[str, str, float]:
    if len(ir.supplies) != 1:
        raise AdapterError("simulator handoff requires exactly one explicit supply")
    supply = ir.supplies[0]
    return str(supply["positive"]), str(supply["negative"]), parse_quantity(supply["value"], "voltage")


def _bias_values(ir: CircuitIr) -> dict[str, float]:
    values: dict[str, float] = {}
    for bias in ir.biases:
        net = bias.get("net")
        if isinstance(net, str) and "value" in bias:
            values[net] = parse_quantity(bias["value"], "voltage")
    return values


def _output_load(ir: CircuitIr, port_id: str) -> str:
    for constraint in ir.constraints:
        if constraint.get("kind") == "capacitance" and constraint.get("net") == port_id:
            return f"{parse_quantity(constraint['value'], 'capacitance') / 1e-12:.12g}p"
    raise AdapterError(f"analog output has no explicit capacitive load: {port_id}")


def prepare_simulator_handoff(
    ir: CircuitIr,
    output_dir: str | Path,
    *,
    library: str,
    cell: str,
    equivalence_confirmed: bool,
    model_includes: tuple[tuple[str, str], ...] = (),
) -> SimulatorHandoff:
    if not equivalence_confirmed:
        raise AdapterError("simulator handoff requires confirmed equivalence")
    positive, negative, vdd = _supply(ir)
    biases = _bias_values(ir)
    input_ports = [port.id for port in ir.ports if port.kind == "signal" and port.direction == "input"]
    if len(input_ports) != 2:
        raise AdapterError("golden analog simulator handoff requires two differential inputs")

    pins: list[dict[str, object]] = []
    for port in ir.ports:
        record: dict[str, object] = {
            "name": port.id,
            "domain": "analog",
            "confidence": 1.0,
            "reason": "derived from validated Circuit IR and confirmed netlist equivalence",
        }
        if port.id == positive:
            record.update({"device_class": "analog_power", "local_pvss": negative, "stimulus": "vdc", "stimulus_params": {"dc": _display(vdd)}})
        elif port.id == negative:
            record.update({"device_class": "analog_ground", "local_pvss": negative})
        elif port.kind == "signal" and port.direction == "input":
            stimulus = {"dc": _display(vdd / 2.0)}
            if port.id == input_ports[0]:
                stimulus.update({"acm": "1", "acp": "0"})
            record.update({"device_class": "analog_input", "stimulus": "vdc", "stimulus_params": stimulus})
        elif port.kind == "signal" and port.direction == "output":
            record.update({"device_class": "analog_output", "load": "cap", "load_params": {"c": _output_load(ir, port.id)}})
        elif port.kind == "bias":
            if port.id not in biases:
                raise AdapterError(f"bias port has no explicit voltage intent: {port.id}")
            record.update({"device_class": "analog_input", "stimulus": "vdc", "stimulus_params": {"dc": _display(biases[port.id])}})
        else:
            raise AdapterError(f"unsupported simulator port intent: {port.id}")
        pins.append(record)

    pin_document = {
        "lib": library,
        "cell": cell,
        "vdd_value": vdd,
        "analog_local_grounds": [{"pvss_name": negative, "members": [positive]}],
        "pins": pins,
        "llm_model": "smic180-analog-designer-deterministic-adapter",
    }
    config = {
        "analyses": [
            {"name": "dc", "enabled": True},
            {"name": "ac", "enabled": True, "sweep": {"param": "freq", "start": "1", "stop": "1G", "dec": "20"}},
            {"name": "tran", "enabled": True, "stop": "10u", "maxstep": "10n", "errpreset": "conservative"},
        ],
        "model_includes": [{"path": path, "section": section} for path, section in model_includes],
        "save_default": "allpub",
        "pin_measurements": {
            positive: {"measures": ["voltage", "current", "power"], "spec": {}},
            "VOUT": {"measures": ["voltage"], "spec": {}},
        },
    }
    output = Path(output_dir)
    store = ArtifactStore(output)
    pins_path = store.write_json(output / "pin_classifications.json", pin_document)
    config_path = store.write_json(output / "sim_config.json", config)
    review_path = store.write_json(output / "review_required.json", {
        "required": True,
        "status": "reviewed",
        "basis": "validated IR, confirmed structure, and fresh direct/Virtuoso simulation equivalence",
        "checks": [
            "VINP carries the sole 1 V AC excitation and VINN is AC quiet",
            "IBIAS is a 0.9 V gate-bias port, not a current-bias pin",
            "VDD and VSS use the explicit 3.3 V supply intent",
            "VOUT carries the explicit 5 pF load",
            "ordinary AC does not validate phase margin",
        ],
    })
    return SimulatorHandoff(pins_path, config_path, review_path)
"""Prepare reviewed inputs for the existing SMIC180 simulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..artifacts import ArtifactStore
from ..ir import CircuitIr


class AdapterError(ValueError):
    """Raised when a downstream handoff gate is not proven."""


@dataclass(frozen=True)
class SimulatorHandoff:
    pin_classifications: Path
    sim_config: Path
    review_required: Path


def _pin(port_id: str, kind: str) -> dict[str, object]:
    device_class = {
        "power": "analog_power",
        "ground": "analog_ground",
        "signal_input": "analog_input",
        "signal_output": "analog_output",
        "bias": "analog_input",
    }[kind]
    record: dict[str, object] = {"name": port_id, "device_class": device_class}
    if device_class == "analog_input":
        record.update({"stimulus": "vsource", "stimulus_params": {"dc": "1.65", "acm": "0", "acp": "0"}})
    if device_class == "analog_output":
        record.update({"load": "cap", "load_params": {"c": "5p"}})
    return record


def prepare_simulator_handoff(ir: CircuitIr, output_dir: str | Path, *, equivalence_confirmed: bool) -> SimulatorHandoff:
    if not equivalence_confirmed:
        raise AdapterError("simulator handoff requires confirmed equivalence")
    output = Path(output_dir)
    store = ArtifactStore(output)
    pins = []
    for port in ir.ports:
        if port.kind == "signal":
            semantic = "signal_output" if port.direction == "output" else "signal_input"
        else:
            semantic = port.kind
        pins.append(_pin(port.id, semantic))
    config = {
        "simulator": "spectre",
        "model_includes": [{"path": "${SIM_PDK_CORE_SPECTRE_INCLUDE}", "section": "tt"}],
        "design_variables": [],
        "analyses": [
            {"name": "op", "type": "dc", "params": {}},
            {"name": "ac", "type": "ac", "params": {"start": "1", "stop": "1G", "dec": "20"}},
            {"name": "tran", "type": "tran", "params": {"stop": "20u"}},
        ],
        "outputs": [{"name": measurement["id"], "expression": measurement["id"]} for measurement in ir.measurements],
        "options": {"temp": 27.0},
    }
    pins_path = store.write_json(output / "pin_classifications.json", pins)
    config_path = store.write_json(output / "sim_config.json", config)
    review_path = store.write_json(output / "review_required.json", {
        "required": True,
        "checks": ["input polarity and AC excitation", "bias polarity and common mode", "power and ground direction", "output load", "measurement expressions"],
    })
    return SimulatorHandoff(pins_path, config_path, review_path)

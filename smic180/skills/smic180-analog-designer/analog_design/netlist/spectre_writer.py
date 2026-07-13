"""Canonical Spectre writer for Circuit IR."""

from __future__ import annotations

from collections.abc import Iterable

from ..ir import CircuitIr, Instance, canonical_ir_digest
from ..technology.base import TechnologyProfile
from .ast import SpectreAnalysis, SpectreDeck, SpectreInclude, SpectreInstance


_TERMINAL_ORDER = {
    "mos.nmos": ("D", "G", "S", "B"),
    "mos.pmos": ("D", "G", "S", "B"),
    "source.current": ("P", "N"),
    "passive.capacitor": ("P", "N"),
    "passive.resistor": ("P", "N"),
}
_PARAMETER_NAMES = {
    "width": "w", "length": "l", "fingers": "nf", "multiplier": "m",
    "capacitance": "c", "resistance": "r", "dc": "dc",
}


def _number(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value)


def _model(master_ref: str) -> str:
    return master_ref.replace(".", "_")


def _profile_instance(item: Instance, technology: TechnologyProfile) -> SpectreInstance:
    adapter = technology.resolve(item.master_ref)
    actual_to_generic = {actual: generic for generic, actual in adapter.terminal_map.items()}
    order = adapter.netlist_terminals or tuple(adapter.terminal_map.values())
    nodes = tuple(item.terminals[actual_to_generic.get(name, name)] for name in order)
    physical = item.physical_parameters
    if adapter.device_class in {"mos.nmos", "mos.pmos"}:
        parameters = {
            adapter.netlist_parameter_map.get("finger_width", "w"): physical["finger_width"],
            adapter.netlist_parameter_map.get("length", "l"): physical["length"],
            adapter.netlist_parameter_map.get("effective_multiplier", "m"): physical["effective_multiplier"],
        }
    elif adapter.device_class == "passive.capacitor":
        parameters = {
            adapter.netlist_parameter_map.get("width", "w"): physical["width"],
            adapter.netlist_parameter_map.get("length", "l"): physical["length"],
            adapter.netlist_parameter_map.get("multiplier", "m"): physical["multiplier"],
        }
    else:
        parameters = {_PARAMETER_NAMES.get(name, name): value for name, value in item.logical_parameters.items()}
    return SpectreInstance(item.id, nodes, adapter.netlist_model or str(adapter.cell), parameters)


def _deck(
    ir: CircuitIr,
    includes: Iterable[tuple[str, str | None]],
    technology: TechnologyProfile | None,
) -> SpectreDeck:
    instances = []
    for item in sorted(ir.instances, key=lambda record: record.id):
        if technology is not None and technology.state == "confirmed":
            instances.append(_profile_instance(item, technology))
            continue
        order = _TERMINAL_ORDER[item.device_class]
        nodes = tuple(item.terminals[name] for name in order)
        parameters = {_PARAMETER_NAMES.get(name, name): value for name, value in item.logical_parameters.items()}
        instances.append(SpectreInstance(item.id, nodes, _model(item.master_ref), parameters))
    analyses = []
    for item in sorted(ir.analyses, key=lambda record: str(record["id"])):
        kind = str(item["type"])
        params = {key: value for key, value in item.items() if key not in {"id", "type"}}
        analyses.append(SpectreAnalysis(str(item["id"]), kind, params))
    title = str(ir.metadata.get("name", "analog_design"))
    return SpectreDeck(
        title=title,
        digest=canonical_ir_digest(ir.source_data),
        includes=tuple(sorted(SpectreInclude(path, section) for path, section in includes)),
        ports=tuple(port.id for port in ir.ports),
        instances=tuple(instances),
        analyses=tuple(analyses),
        saves=tuple(
            name
            for name in ("VINP", "VINN", "VOUT", "VDD", "VSS", "IBIAS")
            if any(port.id == name and port.kind != "ground" for port in ir.ports)
        ),
    )


def _testbench_lines(ir: CircuitIr, deck: SpectreDeck) -> list[str]:
    ground_ports = {port.id for port in ir.ports if port.kind == "ground"}

    def node(name: str) -> str:
        return "0" if name in ground_ports else name

    lines = ["// generated top-level testbench"]
    lines.append(f"X_DUT ({' '.join(node(port.id) for port in ir.ports)}) {deck.title}")
    supply_value = None
    for supply in ir.supplies:
        positive = str(supply["positive"])
        negative = str(supply["negative"])
        value = float(supply["value"])
        supply_value = value if supply_value is None else supply_value
        lines.append(f"{positive}_SRC ({node(positive)} {node(negative)}) vsource dc={_number(value)}")
    common_mode = (supply_value or 0.0) * 0.5
    signal_inputs = [port for port in ir.ports if port.kind == "signal" and port.direction != "output"]
    for index, port in enumerate(signal_inputs):
        phase = 180 if port.id.upper().endswith("N") and len(signal_inputs) > 1 else 0
        magnitude = 0.5 if len(signal_inputs) > 1 else 1.0
        step = -0.01 if phase == 180 else 0.01
        lines.append(
            f"{port.id}_SRC ({port.id} 0) vsource dc={_number(common_mode)} "
            f"type=pulse val0={_number(common_mode)} val1={_number(common_mode + step)} "
            f"delay=2e-6 rise=1e-9 fall=1e-9 width=8e-6 period=20e-6 "
            f"mag={_number(magnitude)} phase={phase}"
        )
    for bias in ir.biases:
        net = str(bias["net"])
        lines.append(f"VBIAS_SRC ({node(net)} 0) vsource dc={_number(float(bias['value']))}")
    for constraint in ir.constraints:
        if constraint.get("kind") == "capacitance":
            lines.append(f"C_LOAD ({node(str(constraint['net']))} 0) capacitor c={_number(float(constraint['value']))}")
    lines.append("")
    return lines


class SpectreWriter:
    def __init__(
        self,
        model_includes: Iterable[tuple[str, str | None]],
        *,
        technology: TechnologyProfile | None = None,
    ) -> None:
        self.model_includes = tuple(model_includes)
        self.technology = technology

    def render(self, ir: CircuitIr) -> str:
        deck = _deck(ir, self.model_includes, self.technology)
        lines = ["simulator lang=spectre", f"// circuit_ir_sha256={deck.digest}", "global 0"]
        for include in deck.includes:
            suffix = f" section={include.section}" if include.section else ""
            lines.append(f'include "{include.path}"{suffix}')
        lines.extend(["", f"subckt {deck.title} {' '.join(deck.ports)}"])
        for instance in deck.instances:
            params = " ".join(f"{name}={_number(value)}" for name, value in sorted(instance.parameters.items()))
            suffix = f" {params}" if params else ""
            lines.append(f"{instance.name} ({' '.join(instance.nodes)}) {instance.model}{suffix}")
        lines.extend([f"ends {deck.title}", ""])
        lines.extend(_testbench_lines(ir, deck))
        for analysis in deck.analyses:
            if analysis.kind == "dc_op":
                lines.append("dcOp dc")
            elif analysis.kind == "ac":
                lines.append(f"{analysis.name} ac start={_number(analysis.parameters['start'])} stop={_number(analysis.parameters['stop'])} dec={_number(analysis.parameters['points_per_decade'])}")
            elif analysis.kind == "tran":
                lines.append(f"{analysis.name} tran stop={_number(analysis.parameters['stop'])}")
            elif analysis.kind == "noise":
                params = " ".join(f"{name}={_number(value)}" for name, value in sorted(analysis.parameters.items()))
                lines.append(f"{analysis.name} noise {params}")
        lines.append(f"save {' '.join(deck.saves)}")
        for supply in ir.supplies:
            lines.append(f"save {supply['positive']}_SRC:p")
        for instance in sorted(ir.instances, key=lambda record: record.id):
            if instance.device_class in {"mos.nmos", "mos.pmos"}:
                lines.append(f"save X_DUT.{instance.id}:oppoint")
        lines.append("saveOptions options save=selected")
        return "\n".join(lines) + "\n"
"""Canonical Spectre writer for Circuit IR."""

from __future__ import annotations

from collections.abc import Iterable

from ..ir import CircuitIr, canonical_ir_digest
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


def _deck(ir: CircuitIr, includes: Iterable[tuple[str, str | None]]) -> SpectreDeck:
    instances = []
    for item in sorted(ir.instances, key=lambda record: record.id):
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
        saves=tuple(name for name in ("VINP", "VINN", "VOUT", "VDD", "VSS") if any(port.id == name for port in ir.ports)),
    )


class SpectreWriter:
    def __init__(self, model_includes: Iterable[tuple[str, str | None]]) -> None:
        self.model_includes = tuple(model_includes)

    def render(self, ir: CircuitIr) -> str:
        deck = _deck(ir, self.model_includes)
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
        for analysis in deck.analyses:
            if analysis.kind == "dc_op":
                lines.append(f"{analysis.name} op")
            elif analysis.kind == "ac":
                lines.append(f"{analysis.name} ac start={_number(analysis.parameters['start'])} stop={_number(analysis.parameters['stop'])} dec={_number(analysis.parameters['points_per_decade'])}")
            elif analysis.kind == "tran":
                lines.append(f"{analysis.name} tran stop={_number(analysis.parameters['stop'])}")
            elif analysis.kind == "noise":
                params = " ".join(f"{name}={_number(value)}" for name, value in sorted(analysis.parameters.items()))
                lines.append(f"{analysis.name} noise {params}")
        lines.append(f"save {' '.join(deck.saves)}")
        lines.append("saveOptions options save=selected")
        return "\n".join(lines) + "\n"


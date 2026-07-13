"""Deterministic two-stage Miller op-amp topology plan."""

from __future__ import annotations

from collections.abc import Mapping

from .base import TopologyError, TopologyInstance, TopologyPlan


def build_two_stage_miller(options: Mapping[str, object]) -> TopologyPlan:
    polarity = options.get("input_pair")
    if polarity not in {"nmos", "pmos"}:
        raise TopologyError("two_stage_miller input_pair must be nmos or pmos")
    input_class = f"mos.{polarity}"
    load_class = "mos.pmos" if polarity == "nmos" else "mos.nmos"
    input_source = "NTAIL"
    input_body = "VSS" if polarity == "nmos" else "VDD"
    load_source = "VDD" if polarity == "nmos" else "VSS"
    load_body = load_source
    second_class = load_class
    instances = (
        TopologyInstance("M_IN_P", "input_pair_positive", input_class, {"D": "N1", "G": "VINP", "S": input_source, "B": input_body}),
        TopologyInstance("M_IN_N", "input_pair_negative", input_class, {"D": "N2", "G": "VINN", "S": input_source, "B": input_body}),
        TopologyInstance("M_LOAD_DIODE", "mirror_diode", load_class, {"D": "N1", "G": "N1", "S": load_source, "B": load_body}),
        TopologyInstance("M_LOAD_OUT", "mirror_output", load_class, {"D": "N2", "G": "N1", "S": load_source, "B": load_body}),
        TopologyInstance("M_TAIL", "tail_source", input_class, {"D": input_source, "G": "IBIAS", "S": input_body, "B": input_body}),
        TopologyInstance("M_SECOND", "second_stage", second_class, {"D": "VOUT", "G": "N2", "S": load_source, "B": load_body}),
        TopologyInstance("M_SECOND_BIAS", "second_stage_bias", input_class, {"D": "VOUT", "G": "IBIAS", "S": input_body, "B": input_body}),
        TopologyInstance("C_MILLER", "miller_compensation", "passive.capacitor", {"P": "VOUT", "N": "N2"}),
        TopologyInstance("R_NULL", "nulling_resistor", "passive.resistor", {"P": "VOUT", "N": "NCOMP"}, enabled=False),
    )
    return TopologyPlan(
        id="two_stage_miller",
        ports=("VDD", "VSS", "VINP", "VINN", "VOUT", "IBIAS"),
        nets=("VDD", "VSS", "VINP", "VINN", "VOUT", "IBIAS", "NTAIL", "N1", "N2", "NCOMP"),
        instances=instances,
        matching_groups={"input_pair": ("M_IN_P", "M_IN_N"), "active_load": ("M_LOAD_DIODE", "M_LOAD_OUT")},
        selection_basis=(
            f"explicit {polarity.upper()} input pair requested",
            "two-stage Miller topology selected as the version-1 golden workflow",
        ),
        known_limits=(
            "phase margin requires a validated STB loop-breaking testbench",
            "initial sizing is an engineering seed, not a PDK-confirmed result",
            "nulling resistor is reserved but disabled in version 1",
        ),
    )


"""Conservative hand-analysis seed for the golden Miller op amp."""

from __future__ import annotations

import math

from ..spec import DesignSpec
from ..topology.base import TopologyPlan
from .base import CalculationRecord, SizingError, SizingResult


def _metric(spec: DesignSpec, identity: str) -> float:
    for metric in spec.metrics:
        if metric.id == identity:
            return metric.value
    raise SizingError(f"required metric is missing: {identity}")


def _record(name: str, formula: str, inputs: dict[str, float], assumptions: tuple[str, ...], dimension: str, value: float, confidence: str = "low") -> CalculationRecord:
    if not math.isfinite(value) or value <= 0:
        raise SizingError(f"calculated {name} is not finite and positive")
    return CalculationRecord(name, formula, inputs, assumptions, dimension, value, "estimate", confidence)


def size_two_stage_miller(spec: DesignSpec, topology: TopologyPlan) -> SizingResult:
    if topology.id != "two_stage_miller":
        raise SizingError("square-law seed only supports two_stage_miller")
    if spec.vdd < 1.0:
        raise SizingError("supply is too low for the version-1 two-stage seed assumptions")
    ugbw = _metric(spec, "ugbw")
    slew = _metric(spec, "slew_rate")
    load = spec.output_capacitance
    if ugbw <= 0 or slew <= 0 or load <= 0:
        raise SizingError("UGBW, slew rate, and load must be positive")

    miller_cap = max(load * 0.22, 0.5e-12)
    gm = 2.0 * math.pi * ugbw * miller_cap
    gm_over_id = 12.0
    current_from_gm = 2.0 * gm / gm_over_id
    current_from_slew = slew * miller_cap
    tail_current = max(current_from_gm, current_from_slew)
    second_current = max(2.0 * tail_current, slew * load)
    overdrive = 0.20
    process_kn_assumption = 180e-6
    branch_current = tail_current / 2.0
    width_over_length = 2.0 * branch_current / (process_kn_assumption * overdrive**2)
    channel_length = 1.0e-6
    input_width = max(width_over_length * channel_length, 1.0e-6)

    common = ("hand-analysis seed only", "must be normalized and confirmed by the live SMIC180 technology profile")
    records = {
        "miller_capacitance": _record("miller_capacitance", "miller_cap_from_load_fraction", {"load": load, "fraction": 0.22}, common + ("minimum seed capacitance is 0.5 pF",), "capacitance", miller_cap, "medium"),
        "input_gm": _record("input_gm", "gm_from_ugbw_and_load", {"ugbw": ugbw, "miller_capacitance": miller_cap}, common + ("gm = 2*pi*UGBW*Cc",), "conductance", gm, "medium"),
        "tail_current": _record("tail_current", "max_gm_id_and_slew_current", {"gm": gm, "gm_over_id": gm_over_id, "slew_rate": slew, "miller_capacitance": miller_cap}, common + ("target gm/Id is 12 1/V",), "current", tail_current),
        "second_stage_current": _record("second_stage_current", "max_second_stage_drive", {"tail_current": tail_current, "slew_rate": slew, "load": load}, common + ("second stage current is at least twice tail current",), "current", second_current),
        "channel_length": _record("channel_length", "conservative_long_channel_seed", {"supply": spec.vdd}, common + ("1 um is a workflow seed, not a PDK minimum or optimum",), "length", channel_length),
        "input_pair_width": _record("input_pair_width", "square_law_width_seed", {"branch_current": branch_current, "kn_assumption": process_kn_assumption, "overdrive": overdrive, "length": channel_length}, common + ("kn=180 uA/V^2 and Vov=0.2 V are unverified seed assumptions",), "length", input_width),
    }
    return SizingResult(records, {})

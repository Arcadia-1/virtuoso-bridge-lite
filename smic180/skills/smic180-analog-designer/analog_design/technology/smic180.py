"""Offline-only SMIC180 profile used before live PDK discovery."""

from __future__ import annotations

import math
import os
from pathlib import PurePosixPath
from typing import Any, Mapping

from .base import DeviceAdapter, TechnologyError, TechnologyProfile
from .discovery import DiscoveryRequest


def _adapter(master_ref: str, device_class: str, terminals: tuple[str, ...], parameters: dict[str, str]) -> DeviceAdapter:
    return DeviceAdapter(
        master_ref=master_ref,
        device_class=device_class,
        library=None,
        cell=None,
        view=None,
        terminals=terminals,
        parameter_map={name: name for name in parameters},
        parameter_dimensions=parameters,
        evidence={},
    )


def create_offline_smic180_profile() -> TechnologyProfile:
    adapters = [
        _adapter("smic180.core_nmos", "mos.nmos", ("D", "G", "S", "B"), {"width": "length", "length": "length", "fingers": "integer", "multiplier": "integer"}),
        _adapter("smic180.core_pmos", "mos.pmos", ("D", "G", "S", "B"), {"width": "length", "length": "length", "fingers": "integer", "multiplier": "integer"}),
        _adapter("smic180.miller_capacitor", "passive.capacitor", ("P", "N"), {"capacitance": "capacitance"}),
        _adapter("smic180.nulling_resistor", "passive.resistor", ("P", "N"), {"resistance": "resistance"}),
        _adapter("analog.current_source", "source.current", ("P", "N"), {"dc": "current"}),
        _adapter("analog.resistor", "passive.resistor", ("P", "N"), {"resistance": "resistance"}),
    ]
    return TechnologyProfile("smic180", "unconfirmed", {item.master_ref: item for item in adapters}, {"purpose": "offline schema and netlist tests only"})



def create_smic180_discovery_request() -> DiscoveryRequest:
    """Return evidence queries for the installed SMIC180 e2r profile."""

    cds_lib = os.getenv("SIM_CDS_LIB", "").strip() or os.getenv("CDS_LIB_PATH_180", "").strip()
    if not cds_lib:
        raise TechnologyError("SMIC180 discovery requires SIM_CDS_LIB or CDS_LIB_PATH_180 from shared site configuration")
    pdk_root = str(PurePosixPath(cds_lib).parent)
    return DiscoveryRequest(
        pdk_roots=(pdk_root,),
        cds_lib_candidates=(cds_lib,),
        device_candidates={
            "smic180.core_nmos": (("smic18ee", "n33e2r", "symbol"),),
            "smic180.core_pmos": (("smic18ee", "p33e2r", "symbol"),),
            "smic180.miller_capacitor": (("smic18ee", "mime2r", "symbol"),),
        },
        model_sections={
            "tt": ("tt", "mim_tt"),
            "ff": ("ff", "mim_ff"),
            "ss": ("ss", "mim_ss"),
            "fnsp": ("fnsp", "mim_tt"),
            "snfp": ("snfp", "mim_tt"),
        },
    )


def physicalize_smic180_instance(
    adapter: DeviceAdapter,
    logical_parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map generic logical values into verified SMIC180 physical/CDF values."""

    if adapter.device_class in {"mos.nmos", "mos.pmos"}:
        width = float(logical_parameters["width"])
        length = float(logical_parameters["length"])
        fingers = int(logical_parameters.get("fingers", 1))
        multiplier = int(logical_parameters.get("multiplier", 1))
        if fingers < 1 or multiplier < 1:
            raise TechnologyError("MOS fingers and multiplier must be positive integers")
        finger_width = float(logical_parameters.get("finger_width", width / fingers))
        if not math.isclose(width, finger_width * fingers, rel_tol=1e-12, abs_tol=1e-18):
            raise TechnologyError("MOS total width must equal finger_width*fingers")
        if length < adapter.limits.get("minimum_length", 0.0):
            raise TechnologyError(f"MOS length is below the confirmed limit for {adapter.master_ref}")
        if finger_width < adapter.limits.get("minimum_finger_width", 0.0):
            raise TechnologyError(f"MOS finger width is below the confirmed limit for {adapter.master_ref}")
        physical = {
            "total_width": width,
            "finger_width": finger_width,
            "length": length,
            "fingers": fingers,
            "multiplier": multiplier,
            "effective_multiplier": multiplier * fingers,
        }
        cdf = {
            adapter.cdf_parameter("width"): width,
            adapter.cdf_parameter("finger_width"): finger_width,
            adapter.cdf_parameter("length"): length,
            adapter.cdf_parameter("fingers"): fingers,
            adapter.cdf_parameter("multiplier"): multiplier,
        }
        return physical, cdf

    if adapter.device_class == "passive.capacitor":
        capacitance = float(logical_parameters["capacitance"])
        density = adapter.limits.get("area_cap_density", 0.0)
        maximum_width = adapter.limits.get("maximum_width", 0.0)
        maximum_length = adapter.limits.get("maximum_length", 0.0)
        if capacitance <= 0 or density <= 0 or maximum_width <= 0 or maximum_length <= 0:
            raise TechnologyError("confirmed MIM density and geometry limits are required")
        multiplier = max(1, math.ceil(capacitance / (density * maximum_width * maximum_length)))
        while True:
            area = capacitance / (density * multiplier)
            width = min(math.sqrt(area), maximum_width)
            length = area / width
            if length <= maximum_length:
                break
            multiplier += 1
        unit_capacitance = density * width * length
        physical = {
            "width": width,
            "length": length,
            "multiplier": multiplier,
            "unit_capacitance": unit_capacitance,
            "effective_capacitance": unit_capacitance * multiplier,
        }
        cdf = {
            "calculatedParam": "Capacitance",
            adapter.cdf_parameter("width"): width,
            adapter.cdf_parameter("length"): length,
            adapter.cdf_parameter("multiplier"): multiplier,
            adapter.cdf_parameter("capacitance"): unit_capacitance,
        }
        return physical, cdf

    raise TechnologyError(f"no SMIC180 physicalization rule for {adapter.device_class}")
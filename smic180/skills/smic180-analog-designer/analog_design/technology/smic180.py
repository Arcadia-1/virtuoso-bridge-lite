"""Offline-only SMIC180 profile used before live PDK discovery."""

from __future__ import annotations

from .base import DeviceAdapter, TechnologyProfile
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

    roots = (
        "/home/IC/Tech/smic18ee_2",
        "/home/IC/Tech/smic18ee_2P6M_20100810",
    )
    return DiscoveryRequest(
        pdk_roots=roots,
        cds_lib_candidates=tuple(f"{root}/cds.lib" for root in roots),
        device_candidates={
            "smic180.core_nmos": (("smic18ee", "n33e2r", "symbol"),),
            "smic180.core_pmos": (("smic18ee", "p33e2r", "symbol"),),
            "smic180.miller_capacitor": (("smic18ee", "mime2r", "symbol"),),
        },
    )

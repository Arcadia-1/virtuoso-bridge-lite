"""Offline-only SMIC180 profile used before live PDK discovery."""

from __future__ import annotations

from .base import DeviceAdapter, TechnologyProfile


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


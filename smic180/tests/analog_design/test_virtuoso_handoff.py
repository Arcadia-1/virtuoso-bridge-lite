import json

import pytest

from analog_design.technology.base import DeviceAdapter, TechnologyProfile
from analog_design.technology.smic180 import create_offline_smic180_profile
from analog_design.virtuoso.materialize import MaterializationError, _validate_readback, materialize_schematic
from analog_design.virtuoso.plan import PlanError, build_schematic_plan
from test_circuit_ir import valid_ir_data
from analog_design.ir import circuit_ir_from_data
from analog_design.builder import build_circuit_ir
from analog_design.sizing.square_law import size_two_stage_miller
from analog_design.topology.registry import default_registry
from test_ir_builder import confirmed_profile as golden_confirmed_profile, load_spec

def confirmed_profile():
    adapters = {}
    for ref, device_class, terminals, params in (
        ("smic180.core_nmos", "mos.nmos", ("D", "G", "S", "B"), {"width": "w", "length": "l"}),
        ("analog.current_source", "source.current", ("P", "N"), {"dc": "dc"}),
        ("analog.resistor", "passive.resistor", ("P", "N"), {"resistance": "r"}),
    ):
        adapters[ref] = DeviceAdapter(ref, device_class, "lib", ref.split(".")[-1], "symbol", terminals, params, {name: "length" if name in {"width", "length"} else "current" if name == "dc" else "resistance" for name in params}, {"master": f"{ref}-master.json", "terminals": f"{ref}-terms.json", "cdf": f"{ref}-cdf.json"})
    return TechnologyProfile("smic180", "confirmed", adapters, {"pdk_root": "/pdk", "cds_lib": "/pdk/cds.lib"})


class FakeVirtuosoClient:
    def __init__(self, *, exists=False, readback=None, schcheck=True):
        self.exists = exists
        self.readback = readback
        self.schcheck = schcheck
        self.calls = []

    def cell_exists(self, library, cell, view):
        self.calls.append(("exists", library, cell, view))
        return self.exists

    def preflight_master(self, library, cell, view, terminals):
        self.calls.append(("preflight", library, cell, view, tuple(terminals)))
        return True

    def create_schematic(self, plan):
        self.calls.append(("create", plan.target_cell))

    def apply_cdf(self, plan):
        self.calls.append(("cdf", plan.target_cell))

    def save_close(self, library, cell):
        self.calls.append(("save_close", library, cell))

    def reopen_readback(self, plan):
        self.calls.append(("readback", plan.target_cell))
        return self.readback if self.readback is not None else plan.expected_readback

    def schcheck_save(self, library, cell):
        self.calls.append(("schcheck", library, cell))
        return self.schcheck

    def export_si(self, library, cell, output):
        self.calls.append(("export", library, cell, str(output)))
        output.write_text("simulator lang=spectre\n", encoding="utf-8")
        return output


def test_plan_refuses_unconfirmed_profile():
    with pytest.raises(PlanError, match="confirmed"):
        build_schematic_plan(circuit_ir_from_data(valid_ir_data()), create_offline_smic180_profile(), "design_lib", "miller_target", source_cell="source")


def test_plan_resolves_masters_terminals_and_expected_cdf_values():
    plan = build_schematic_plan(circuit_ir_from_data(valid_ir_data()), confirmed_profile(), "design_lib", "miller_target", source_cell="source")
    assert plan.instances[0].library == "lib"
    assert plan.instances[0].terminals["G"] == "VINP"
    assert plan.expected_readback["M1"]["w"] == pytest.approx(10e-6)


def test_plan_requires_source_and_target_to_differ():
    with pytest.raises(PlanError, match="source and target"):
        build_schematic_plan(circuit_ir_from_data(valid_ir_data()), confirmed_profile(), "design_lib", "same", source_cell="same")


def test_materialize_plan_only_performs_no_client_calls(tmp_path):
    plan = build_schematic_plan(circuit_ir_from_data(valid_ir_data()), confirmed_profile(), "design_lib", "target", source_cell="source")
    client = FakeVirtuosoClient()
    result = materialize_schematic(client, plan, tmp_path, plan_only=True)
    assert result["status"] == "planned"
    assert client.calls == []


def test_materialize_refuses_existing_target_without_replace(tmp_path):
    plan = build_schematic_plan(circuit_ir_from_data(valid_ir_data()), confirmed_profile(), "design_lib", "target", source_cell="source")
    with pytest.raises(MaterializationError, match="already exists"):
        materialize_schematic(FakeVirtuosoClient(exists=True), plan, tmp_path)


def test_materialize_requires_close_reopen_cdf_schcheck_and_export(tmp_path):
    plan = build_schematic_plan(circuit_ir_from_data(valid_ir_data()), confirmed_profile(), "design_lib", "target", source_cell="source")
    client = FakeVirtuosoClient()
    evidence = materialize_schematic(client, plan, tmp_path)
    names = [call[0] for call in client.calls]
    assert names.index("cdf") < names.index("save_close") < names.index("readback") < names.index("schcheck") < names.index("export")
    assert evidence["cdf_roundtrip_passed"] is True
    assert evidence["schematic_checked"] is True
    assert (tmp_path / "exported_netlist.scs").is_file()


def test_materialize_rejects_cdf_mismatch_and_failed_schcheck(tmp_path):
    plan = build_schematic_plan(circuit_ir_from_data(valid_ir_data()), confirmed_profile(), "design_lib", "target", source_cell="source")
    with pytest.raises(MaterializationError, match="CDF readback"):
        materialize_schematic(FakeVirtuosoClient(readback={}), plan, tmp_path / "cdf")
    with pytest.raises(MaterializationError, match="schCheck"):
        materialize_schematic(FakeVirtuosoClient(schcheck=False), plan, tmp_path / "check")

def test_golden_plan_maps_generic_terminals_and_uses_frozen_cdf_expectations(tmp_path):
    spec = load_spec(tmp_path)
    topology = default_registry().create("two_stage_miller", spec.interfaces)
    profile = golden_confirmed_profile()
    ir = build_circuit_ir(spec, topology, size_two_stage_miller(spec, topology), profile)

    plan = build_schematic_plan(ir, profile, "analog_design", "golden_target", source_cell="golden_source")

    planned = {item.id: item for item in plan.instances}
    assert planned["C_MILLER"].terminals == {"PLUS": "VOUT", "MINUS": "N2"}
    assert planned["C_MILLER"].cdf_values == ir.instance("C_MILLER").cdf_expectations
    assert planned["M_IN_P"].cdf_values == ir.instance("M_IN_P").cdf_expectations
    assert set(planned["M_IN_P"].cdf_values) == {"w", "fw", "l", "fingers", "m"}
    assert planned["M_IN_P"].cdf_dimensions["fw"] == "length"
    assert plan.ports["VOUT"] == "output"
    assert set(plan.nets) >= {"VDD", "VSS", "VINP", "VINN", "VOUT", "IBIAS", "N1", "N2", "NTAIL"}

def test_cdf_readback_accepts_only_pdk_display_resolution_normalization():
    expected = {"M1": {"w": 1.59988514766e-6}}
    _validate_readback(expected, {"M1": {"w": {"value": 1.6e-6, "raw": "1.6u", "resolution": 1e-7}}})
    with pytest.raises(MaterializationError, match="CDF readback mismatch"):
        _validate_readback(expected, {"M1": {"w": {"value": 1.8e-6, "raw": "1.8u", "resolution": 1e-7}}})

def test_failed_materialization_clears_stale_confirmation_markers(tmp_path):
    plan = build_schematic_plan(circuit_ir_from_data(valid_ir_data()), confirmed_profile(), "design_lib", "target", source_cell="source")
    materialize_schematic(FakeVirtuosoClient(), plan, tmp_path)
    assert (tmp_path / "cdf_roundtrip.confirmed.json").is_file()
    assert (tmp_path / "schematic_checked.confirmed.json").is_file()

    with pytest.raises(MaterializationError, match="CDF readback"):
        materialize_schematic(FakeVirtuosoClient(readback={}), plan, tmp_path)

    assert not (tmp_path / "cdf_roundtrip.confirmed.json").exists()
    assert not (tmp_path / "schematic_checked.confirmed.json").exists()

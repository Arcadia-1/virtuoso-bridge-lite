import json

import pytest

from analog_design.technology.base import DeviceAdapter, TechnologyProfile
from analog_design.technology.smic180 import create_offline_smic180_profile
from analog_design.virtuoso.materialize import MaterializationError, materialize_schematic
from analog_design.virtuoso.plan import PlanError, build_schematic_plan
from test_circuit_ir import valid_ir_data
from analog_design.ir import circuit_ir_from_data


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

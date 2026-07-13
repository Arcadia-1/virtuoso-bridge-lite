from pathlib import Path

from analog_design.builder import build_circuit_ir
from analog_design.sizing.square_law import size_two_stage_miller
from analog_design.topology.registry import default_registry
from analog_design.virtuoso.live import NativeVirtuosoMaterializationClient
from analog_design.virtuoso.plan import build_schematic_plan
from test_ir_builder import confirmed_profile, load_spec


class Result:
    def __init__(self, output="NATIVE_OK", errors=()):
        self.output = output
        self.errors = list(errors)


class FakeBridge:
    def __init__(self):
        self.expressions = []

    def execute_skill(self, expression, timeout=30):
        self.expressions.append(expression)
        for marker in ("NATIVE_INSTANCES_OK", "NATIVE_CREATE_OK", "NATIVE_CDF_OK", "NATIVE_CLOSE_OK", "NATIVE_SCHCHECK_OK"):
            if marker in expression:
                return Result(marker)
        return Result()


def golden_plan(tmp_path):
    spec = load_spec(tmp_path)
    topology = default_registry().create("two_stage_miller", spec.interfaces)
    profile = confirmed_profile()
    ir = build_circuit_ir(spec, topology, size_two_stage_miller(spec, topology), profile)
    return build_schematic_plan(ir, profile, "amp_text", "codex_miller", source_cell="frozen_ir")


def test_native_client_creates_database_connectivity_without_gui_pin_or_label_calls(tmp_path):
    bridge = FakeBridge()
    client = NativeVirtuosoMaterializationClient(bridge, exporter=lambda *args: Path(args[-1]))

    client.create_schematic(golden_plan(tmp_path))

    assert len(bridge.expressions) == 2
    create_skill, connect_skill = bridge.expressions
    assert 'dbOpenCellViewByType("amp_text" "codex_miller" "schematic" "schematic" "w")' in create_skill
    assert 'dbCreateInst(cv master "C_MILLER"' in create_skill
    assert "dbMergeNet" not in create_skill
    assert 'dbOpenCellViewByType("amp_text" "codex_miller" "schematic" "schematic" "a")' in connect_skill
    assert 'schCreateWireLabel(cv nil' in connect_skill
    assert '"C_MILLER"' in connect_skill and '"PLUS"' in connect_skill and '"VOUT"' in connect_skill
    assert 'dbOpenCellViewByType("basic" "ipin" "symbol" nil "r")' in connect_skill
    assert 'dbOpenCellViewByType("basic" "opin" "symbol" nil "r")' in connect_skill
    assert 'dbCreateTerm(net "VDD" "input")' in connect_skill
    assert 'dbCreateTerm(net "VOUT" "output")' in connect_skill
    assert 'dbCreatePin(net pinFig)' in connect_skill
    assert "dbCreateInstTerm" not in connect_skill
    assert "dbMergeNet" not in connect_skill
    assert "dbCreateConn" not in connect_skill
    assert "schCreatePin" not in create_skill + connect_skill


def test_native_client_uses_verified_pas_cdf_sequence_and_pdk_native_units(tmp_path):
    bridge = FakeBridge()
    client = NativeVirtuosoMaterializationClient(bridge, exporter=lambda *args: Path(args[-1]))

    client.apply_cdf(golden_plan(tmp_path))

    skill = bridge.expressions[-1]
    assert "PasCdfFormInit(iCDF)" in skill
    assert 'PasCdfSetValue(get(iCDF "w") "1.59988514766u")' in skill
    assert 'PasCdfSetValue(get(iCDF "l") "1u")' in skill
    assert 'PasCdfSetValue(get(iCDF "fingers") "1")' in skill
    assert 'PasCdfSetValue(get(iCDF "c")' in skill and skill.count("f\"") >= 1
    assert "PasCdfCallCallbacks(inst" in skill
    assert "PasCdfDone(inst)" in skill
    assert "setInstParams" not in skill
class EncodedBridge(FakeBridge):
    def execute_skill(self, expression, timeout=30):
        self.expressions.append(expression)
        return Result('"D\\nG\\nB\\nS\\n"')


def test_native_client_decodes_multiline_bridge_output_for_master_preflight():
    client = NativeVirtuosoMaterializationClient(EncodedBridge(), exporter=lambda *args: Path(args[-1]))
    assert client.preflight_master("smic18ee", "n33e2r", "symbol", ("D", "G", "B", "S")) is True
class ReadbackBridge(FakeBridge):
    def execute_skill(self, expression, timeout=30):
        self.expressions.append(expression)
        return Result('"M_IN_P|w|\\"1.6u\\"\\\\nM_IN_P|fw|\\"1.6u\\"\\\\n"')


def test_native_client_decodes_literal_newlines_in_cdf_readback(tmp_path):
    plan = golden_plan(tmp_path)
    only = type(plan)(
        plan.library,
        plan.target_cell,
        plan.source_cell,
        plan.view,
        plan.ports,
        plan.nets,
        tuple(item for item in plan.instances if item.id == "M_IN_P"),
        {"M_IN_P": {"w": plan.expected_readback["M_IN_P"]["w"], "fw": plan.expected_readback["M_IN_P"]["fw"]}},
    )
    item = only.instances[0]
    trimmed = type(item)(item.id, item.library, item.cell, item.view, item.terminals, {"w": item.cdf_values["w"], "fw": item.cdf_values["fw"]}, {"w": "length", "fw": "length"})
    only = type(plan)(only.library, only.target_cell, only.source_cell, only.view, only.ports, only.nets, (trimmed,), only.expected_readback)
    client = NativeVirtuosoMaterializationClient(ReadbackBridge(), exporter=lambda *args: Path(args[-1]))

    readback = client.reopen_readback(only)

    assert readback == {
        "M_IN_P": {
            "w": {"value": 1.6e-6, "raw": "1.6u", "resolution": 1e-7},
            "fw": {"value": 1.6e-6, "raw": "1.6u", "resolution": 1e-7},
        }
    }

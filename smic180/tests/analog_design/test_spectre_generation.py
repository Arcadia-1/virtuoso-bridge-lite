from analog_design.builder import build_circuit_ir
from analog_design.ir import canonical_ir_digest
from analog_design.netlist.spectre_writer import SpectreWriter
from analog_design.sizing.square_law import size_two_stage_miller
from analog_design.technology.smic180 import create_offline_smic180_profile
from analog_design.topology.registry import default_registry
from test_ir_builder import load_spec


def make_ir(tmp_path):
    spec = load_spec(tmp_path)
    topology = default_registry().create("two_stage_miller", spec.interfaces)
    return build_circuit_ir(spec, topology, size_two_stage_miller(spec, topology), create_offline_smic180_profile())


def test_equal_ir_emits_byte_identical_canonical_spectre(tmp_path):
    ir = make_ir(tmp_path)
    writer = SpectreWriter(model_includes=(("/models/core.scs", "tt"),))
    first = writer.render(ir)
    second = writer.render(ir)
    assert first == second
    assert first.startswith("simulator lang=spectre\n")
    assert f"// circuit_ir_sha256={canonical_ir_digest(ir.source_data)}" in first


def test_spectre_order_and_number_format_are_stable(tmp_path):
    text = SpectreWriter(model_includes=(("/z.scs", "tt"), ("/a.scs", None))).render(make_ir(tmp_path))
    assert text.index('include "/a.scs"') < text.index('include "/z.scs" section=tt')
    assert text.index("subckt golden_miller") < text.index("op op")
    assert "1e-06" in text
    assert "save VINP VINN VOUT VDD VSS" in text
    assert "saveOptions options save=selected" in text


def test_writer_emits_requested_dc_ac_and_transient_analyses(tmp_path):
    text = SpectreWriter(model_includes=()).render(make_ir(tmp_path))
    assert "op op" in text
    assert "ac ac start=" in text
    assert "tran tran stop=" in text
    assert "phase_margin" not in text

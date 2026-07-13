from analog_design.builder import build_circuit_ir
from analog_design.ir import canonical_ir_digest
from analog_design.netlist.spectre_writer import SpectreWriter
from analog_design.sizing.square_law import size_two_stage_miller
from analog_design.technology.smic180 import create_offline_smic180_profile
from analog_design.topology.registry import default_registry
from test_ir_builder import confirmed_profile, load_spec


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
    assert text.index("subckt golden_miller") < text.index("dcOp dc")
    assert "1e-06" in text
    assert "save VINP VINN VOUT VDD IBIAS" in text
    assert " VSS" not in next(line for line in text.splitlines() if line.startswith("save ") and ":oppoint" not in line)
    assert "save VDD_SRC:p" in text
    assert "saveOptions options save=selected" in text


def test_writer_emits_requested_dc_ac_and_transient_analyses(tmp_path):
    text = SpectreWriter(model_includes=()).render(make_ir(tmp_path))
    assert "dcOp dc" in text
    assert "ac ac start=" in text
    assert "tran tran stop=" in text
    assert "phase_margin" not in text
    assert "save X_DUT.M_IN_P:oppoint" in text
    assert "save X_DUT.M_SECOND_BIAS:oppoint" in text


def test_confirmed_profile_emits_executable_real_smic180_testbench(tmp_path):
    spec = load_spec(tmp_path)
    topology = default_registry().create("two_stage_miller", spec.interfaces)
    ir = build_circuit_ir(spec, topology, size_two_stage_miller(spec, topology), confirmed_profile())
    profile = confirmed_profile()
    model_path = "/home/IC/Tech/smic18ee_2P6M_20100810/models/spectre/e2r018_v1p8_spe.scs"
    text = SpectreWriter(
        model_includes=profile.model_includes(model_path, "tt"),
        technology=profile,
    ).render(ir)
    assert f'include "{model_path}" section=tt' in text
    assert f'include "{model_path}" section=mim_tt' in text
    assert "M_IN_P (N1 VINP NTAIL VSS) n33e2r" in text
    assert "M_LOAD_DIODE (N1 N1 VDD VDD) p33e2r" in text
    assert "M_SECOND_BIAS (VOUT IBIAS VSS VSS) n33e2r" in text
    assert "C_MILLER (VOUT N2) mime2r" in text
    assert " nf=" not in text
    assert "X_DUT (VDD 0 VINP VINN VOUT IBIAS) golden_miller" in text
    assert "VDD_SRC (VDD 0) vsource dc=3.3" in text
    assert "VINP_SRC (VINP 0) vsource" in text and "mag=0.5" in text and "phase=0" in text
    assert "VINN_SRC (VINN 0) vsource" in text and "phase=180" in text
    assert "VBIAS_SRC (IBIAS 0) vsource dc=0.9" in text
    assert "C_LOAD (VOUT 0) capacitor c=5e-12" in text
    assert text.index("X_DUT") < text.index("dcOp dc")
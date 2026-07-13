import math

import pytest

from analog_design.netlist.equivalence import EquivalenceError, build_virtuoso_replay_deck, compare_metrics, compare_netlists, write_equivalence_confirmation
from analog_design.netlist.spectre_reader import NetlistParseError, parse_spectre_circuit


DIRECT = """simulator lang=spectre
subckt amp VINP VINN VOUT VDD VSS
M1 (N1 VINP NTAIL VSS) nch w=1e-05 l=1e-06
M2 (VOUT VINN NTAIL VSS) nch l=1e-06 w=1e-05
C1 (VOUT N1) capacitor c=1e-12
ends amp
"""

EXPORTED = """simulator lang=spectre
subckt amp VINP VINN VOUT VDD VSS
C1 (VOUT N1) capacitor c=1p
M2 (VOUT VINN NTAIL VSS) nch w=10u l=1u m=1
M1 (N1 VINP NTAIL VSS) nch l=1u w=10u m=1
ends amp
"""


def test_parser_extracts_subcircuit_ports_instances_nodes_models_and_parameters():
    circuit = parse_spectre_circuit(DIRECT)
    assert circuit.name == "amp"
    assert circuit.ports == ("VINP", "VINN", "VOUT", "VDD", "VSS")
    assert circuit.instances["M1"].nodes == ("N1", "VINP", "NTAIL", "VSS")
    assert circuit.instances["M1"].parameters["w"] == pytest.approx(10e-6)


def test_semantic_comparison_tolerates_order_suffixes_and_explicit_defaults():
    result = compare_netlists(DIRECT, EXPORTED, parameter_defaults={"nch": {"m": 1.0}})
    assert result["equivalent"] is True


def test_semantic_comparison_reports_connectivity_model_and_parameter_mismatch():
    changed = EXPORTED.replace("(VOUT VINN NTAIL VSS)", "(N1 VINN NTAIL VSS)")
    result = compare_netlists(DIRECT, changed, parameter_defaults={"nch": {"m": 1.0}})
    assert result["equivalent"] is False
    assert any("M2" in item and "nodes" in item for item in result["differences"])


def test_parser_rejects_unsupported_or_missing_subcircuit():
    with pytest.raises(NetlistParseError):
        parse_spectre_circuit("simulator lang=spectre\n")


def test_metric_comparison_uses_absolute_and_relative_tolerances():
    result = compare_metrics({"gain": 60.0, "ugbw": 10e6}, {"gain": 60.01, "ugbw": 10.05e6}, {"gain": {"abs": 0.02, "rel": 0.0}, "ugbw": {"abs": 0.0, "rel": 0.01}})
    assert result["equivalent"] is True
    failed = compare_metrics({"gain": 60.0}, {"gain": 61.0}, {"gain": {"abs": 0.1, "rel": 0.0}})
    assert failed["equivalent"] is False


def test_metric_comparison_rejects_missing_nonfinite_and_stale_results():
    with pytest.raises(EquivalenceError, match="missing"):
        compare_metrics({"gain": 60.0}, {}, {"gain": {"abs": 0.1, "rel": 0.0}})
    with pytest.raises(EquivalenceError, match="finite"):
        compare_metrics({"gain": 60.0}, {"gain": math.nan}, {"gain": {"abs": 0.1, "rel": 0.0}})
    with pytest.raises(EquivalenceError, match="fresh"):
        compare_metrics({"gain": 60.0}, {"gain": 60.0}, {"gain": {"abs": 0.1, "rel": 0.0}}, fresh=False)


def test_confirmation_is_written_only_when_both_equivalence_checks_pass(tmp_path):
    structural = compare_netlists(DIRECT, EXPORTED, parameter_defaults={"nch": {"m": 1.0}})
    metrics = compare_metrics({"gain": 60.0}, {"gain": 60.01}, {"gain": {"abs": 0.02, "rel": 0.0}})
    marker = write_equivalence_confirmation(tmp_path, structural, metrics)
    assert marker.is_file()
    with pytest.raises(EquivalenceError, match="both"):
        write_equivalence_confirmation(tmp_path / "failed", {"equivalent": False}, metrics)

FLAT_EXPORTED = """// Cell name: amp_target
M1 (N1 VINP NTAIL VSS) nch w=(10u) l=1u as=1p ad=1p \\
        ps=2u pd=2u m=(1)*(1)
M2 (VOUT VINN NTAIL VSS) nch l=1u w=(10.04u) m=(1)*(1)
C1 (VOUT N1) capacitor c=1p
"""


def test_parser_supports_flat_si_output_continuations_and_parenthesized_products():
    circuit = parse_spectre_circuit(
        FLAT_EXPORTED,
        flat_name="amp",
        flat_ports=("VINP", "VINN", "VOUT", "VDD", "VSS"),
    )
    assert circuit.name == "amp"
    assert circuit.ports == ("VINP", "VINN", "VOUT", "VDD", "VSS")
    assert circuit.instances["M1"].parameters["w"] == pytest.approx(10e-6)
    assert circuit.instances["M1"].parameters["m"] == pytest.approx(1.0)
    assert circuit.instances["M1"].parameters["ps"] == pytest.approx(2e-6)


def test_semantic_comparison_supports_flat_si_defaults_ignored_parameters_and_cdf_resolution():
    result = compare_netlists(
        DIRECT,
        FLAT_EXPORTED,
        right_flat_name="amp",
        right_flat_ports=("VINP", "VINN", "VOUT", "VDD", "VSS"),
        parameter_defaults={"nch": {"m": 1.0}},
        ignored_parameters={"nch": {"as", "ad", "ps", "pd"}},
        parameter_tolerances={"M2.w": {"abs": 0.1e-6, "rel": 0.0}},
    )
    assert result["equivalent"] is True

def test_replay_deck_wraps_exported_body_without_editing_devices_and_reuses_direct_harness():
    direct_deck = DIRECT + "X_DUT (VINP VINN VOUT VDD VSS) amp\nac ac start=1 stop=1G dec=20\n"
    replay = build_virtuoso_replay_deck(direct_deck, FLAT_EXPORTED)
    assert "subckt amp VINP VINN VOUT VDD VSS" in replay
    assert "M1 (N1 VINP NTAIL VSS) nch w=(10u) l=1u as=1p ad=1p" in replay
    assert "X_DUT (VINP VINN VOUT VDD VSS) amp" in replay
    assert "ac ac start=1 stop=1G dec=20" in replay
    assert replay.count("subckt amp") == 1
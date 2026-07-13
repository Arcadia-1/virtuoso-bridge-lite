import pytest

from analog_design.technology.base import (
    DeviceAdapter,
    TechnologyError,
    TechnologyProfile,
    load_technology_profile,
    write_technology_profile,
)
from analog_design.technology.smic180 import create_offline_smic180_profile


def test_profile_resolves_stable_master_ref_and_terminal_order():
    profile = create_offline_smic180_profile()
    adapter = profile.resolve("smic180.core_nmos")
    assert adapter.device_class == "mos.nmos"
    assert adapter.terminals == ("D", "G", "S", "B")


def test_adapter_maps_generic_parameter_to_cdf_and_normalizes_value():
    adapter = DeviceAdapter(
        master_ref="test.nmos",
        device_class="mos.nmos",
        library="testlib",
        cell="nmos",
        view="symbol",
        terminals=("D", "G", "S", "B"),
        parameter_map={"width": "w"},
        parameter_dimensions={"width": "length"},
        evidence={"master": "probe.json", "terminals": "probe.json", "cdf": "roundtrip.json"},
    )
    assert adapter.cdf_parameter("width") == "w"
    assert adapter.normalize("width", "10um") == pytest.approx(10e-6)


def test_unknown_master_ref_and_parameter_are_rejected():
    profile = create_offline_smic180_profile()
    with pytest.raises(TechnologyError, match="unknown master_ref"):
        profile.resolve("smic180.no_such_device")
    with pytest.raises(TechnologyError, match="parameter"):
        profile.resolve("smic180.core_nmos").cdf_parameter("mystery")


def test_unconfirmed_profile_refuses_live_use():
    profile = create_offline_smic180_profile()
    assert profile.state == "unconfirmed"
    with pytest.raises(TechnologyError, match="confirmed"):
        profile.require_live_ready()


def test_confirmed_profile_requires_complete_evidence():
    adapter = DeviceAdapter(
        master_ref="test.nmos",
        device_class="mos.nmos",
        library="testlib",
        cell="nmos",
        view="symbol",
        terminals=("D", "G", "S", "B"),
        parameter_map={"width": "w"},
        parameter_dimensions={"width": "length"},
        evidence={"master": "probe.json"},
    )
    with pytest.raises(TechnologyError, match="evidence"):
        TechnologyProfile("test", "confirmed", {adapter.master_ref: adapter}, {"pdk_root": "/pdk"})


def test_confirmed_profile_accepts_evidence_backed_adapter():
    adapter = DeviceAdapter(
        master_ref="test.nmos",
        device_class="mos.nmos",
        library="testlib",
        cell="nmos",
        view="symbol",
        terminals=("D", "G", "S", "B"),
        parameter_map={"width": "w"},
        parameter_dimensions={"width": "length"},
        evidence={"master": "master.json", "terminals": "terms.json", "cdf": "roundtrip.json"},
    )
    profile = TechnologyProfile("test", "confirmed", {adapter.master_ref: adapter}, {"pdk_root": "/pdk", "cds_lib": "/pdk/cds.lib"})
    profile.require_live_ready()


def test_dimensionless_integer_device_parameters_remain_integer():
    adapter = create_offline_smic180_profile().resolve("smic180.core_nmos")
    assert adapter.normalize("fingers", 4) == 4
    with pytest.raises(TechnologyError, match="integer"):
        adapter.normalize("fingers", 4.5)


def test_confirmed_profile_roundtrip_preserves_netlist_semantics_and_limits(tmp_path):
    adapter = DeviceAdapter(
        master_ref="smic180.core_nmos",
        device_class="mos.nmos",
        library="smic18ee",
        cell="n33e2r",
        view="symbol",
        terminals=("D", "G", "B", "S"),
        parameter_map={"width": "w", "finger_width": "fw", "length": "l", "fingers": "fingers", "multiplier": "m"},
        parameter_dimensions={"width": "length", "finger_width": "length", "length": "length", "fingers": "integer", "multiplier": "integer"},
        evidence={"master": "master.json", "terminals": "terminals.json", "cdf": "roundtrip.json"},
        terminal_map={"D": "D", "G": "G", "S": "S", "B": "B"},
        netlist_model="n33e2r",
        netlist_terminals=("D", "G", "S", "B"),
        netlist_parameter_map={"finger_width": "w", "length": "l", "effective_multiplier": "m"},
        parameter_relations={"width": "finger_width*fingers", "effective_multiplier": "multiplier*fingers"},
        limits={"minimum_length": 600e-9, "minimum_finger_width": 600e-9},
    )
    profile = TechnologyProfile(
        "smic180",
        "confirmed",
        {adapter.master_ref: adapter},
        {"pdk_root": "/home/IC/Tech/smic18ee_2P6M_20100810", "cds_lib": "/home/IC/Tech/smic18ee_2P6M_20100810/cds.lib"},
        model_sections={"tt": ("tt", "mim_tt")},
    )
    path = tmp_path / "technology_profile.json"
    write_technology_profile(path, profile)
    loaded = load_technology_profile(path)
    resolved = loaded.resolve("smic180.core_nmos")
    assert resolved.terminal_map == {"D": "D", "G": "G", "S": "S", "B": "B"}
    assert resolved.netlist_model == "n33e2r"
    assert resolved.netlist_terminals == ("D", "G", "S", "B")
    assert resolved.parameter_relations["effective_multiplier"] == "multiplier*fingers"
    assert resolved.limits["minimum_length"] == pytest.approx(600e-9)
    assert loaded.model_includes("/models/e2r018_v1p8_spe.scs", "tt") == (
        ("/models/e2r018_v1p8_spe.scs", "tt"),
        ("/models/e2r018_v1p8_spe.scs", "mim_tt"),
    )
    loaded.require_live_ready()

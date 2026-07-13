import pytest

from analog_design.topology.base import TopologyError
from analog_design.topology.registry import default_registry


def test_unknown_topology_is_rejected():
    with pytest.raises(TopologyError, match="unknown topology"):
        default_registry().create("no_such_topology", {"input_pair": "nmos"})


def test_two_stage_miller_is_registered_and_explainable():
    plan = default_registry().create("two_stage_miller", {"input_pair": "nmos"})
    assert plan.id == "two_stage_miller"
    assert plan.selection_basis
    assert plan.known_limits


@pytest.mark.parametrize("polarity,device_class,load_class", [("nmos", "mos.nmos", "mos.pmos"), ("pmos", "mos.pmos", "mos.nmos")])
def test_input_pair_polarity_is_explicit(polarity, device_class, load_class):
    plan = default_registry().create("two_stage_miller", {"input_pair": polarity})
    positive = plan.instance("M_IN_P")
    negative = plan.instance("M_IN_N")
    assert positive.device_class == negative.device_class == device_class
    assert plan.instance("M_LOAD_DIODE").device_class == load_class


def test_invalid_input_pair_polarity_is_rejected():
    with pytest.raises(TopologyError, match="input_pair"):
        default_registry().create("two_stage_miller", {"input_pair": "auto"})


def test_miller_plan_has_required_ports_roles_and_matching_groups():
    plan = default_registry().create("two_stage_miller", {"input_pair": "nmos"})
    assert set(plan.ports) == {"VDD", "VSS", "VINP", "VINN", "VOUT", "IBIAS"}
    roles = {item.role for item in plan.instances}
    assert {"input_pair_positive", "input_pair_negative", "mirror_diode", "mirror_output", "tail_source", "second_stage", "miller_compensation"}.issubset(roles)
    assert plan.matching_groups["input_pair"] == ("M_IN_P", "M_IN_N")
    assert plan.matching_groups["active_load"] == ("M_LOAD_DIODE", "M_LOAD_OUT")


def test_nulling_resistor_slot_is_present_but_disabled():
    plan = default_registry().create("two_stage_miller", {"input_pair": "nmos"})
    slot = plan.instance("R_NULL")
    assert slot.enabled is False
    assert slot.role == "nulling_resistor"

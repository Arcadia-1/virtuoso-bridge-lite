from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_net_stub,
    schematic_create_net_expression,
    schematic_set_netset_property,
)


def test_schematic_create_net_expression_attaches_expression_to_net_wire() -> None:
    skill = schematic_create_net_expression(
        "VDD",
        "[@vdd:%:vdd!]",
        1.25,
        -0.5,
        justification="centerLeft",
        rotation="R90",
        font_style="stick",
        height=0.0625,
    )

    assert 'x~>net && x~>net~>name == "VDD"' in skill
    assert 'unless(rbWire error("wire for net not found"))' in skill
    assert (
        'schCreateNetExpression(cv "[@vdd:%:vdd!]" rbWire '
        '\'(1.250 -0.500) "centerLeft" "R90" "stick" 0.0625)'
    ) in skill


def test_schematic_create_net_expression_accepts_custom_cellview_expr() -> None:
    skill = schematic_create_net_expression(
        "VSS",
        "[@vss:%:gnd!]",
        0,
        0,
        cv_expr="targetCv",
    )

    assert "targetCv~>shapes" in skill
    assert 'schCreateNetExpression(targetCv "[@vss:%:gnd!]" rbWire' in skill


def test_schematic_set_netset_property_writes_inherited_override() -> None:
    skill = schematic_set_netset_property("XI0", "vdd", "VDD")

    assert 'x~>name == "XI0"' in skill
    assert 'unless(rbInst error("instance not found"))' in skill
    assert 'dbReplaceProp(rbInst "vdd" "netSet" "VDD")' in skill


def test_schematic_set_netset_property_escapes_string_literals() -> None:
    skill = schematic_set_netset_property('XI"0', "bulk\\net", 'VDD"TOP')

    assert 'x~>name == "XI\\"0"' in skill
    assert 'dbReplaceProp(rbInst "bulk\\\\net" "netSet" "VDD\\"TOP")' in skill


def test_schematic_create_net_stub_draws_short_wire_and_label() -> None:
    skill = schematic_create_net_stub("IN", 0, 0, direction="right", length=0.5)

    assert 'schCreateWire(cv "route" "full" \'((0.000 0.000) (0.500 0.000)) 0 0 0 nil nil)' in skill
    assert 'schCreateWireLabel(cv nil \'(0.250 0.000) "IN" "centerCenter" "R0" "stick" 0.0625 nil)' in skill


def test_schematic_create_net_stub_auto_rotates_vertical_labels() -> None:
    skill = schematic_create_net_stub("VDD", 1, 2, direction="up", length=0.75)

    assert "'((1.000 2.000) (1.000 2.750))" in skill
    assert '\'(1.000 2.375) "VDD" "centerCenter" "R90"' in skill


def test_schematic_create_net_stub_escapes_label_text_and_overrides_rotation() -> None:
    skill = schematic_create_net_stub('A"NET\\1', 0, 0, direction="up", rotation="R0")

    assert '"A\\"NET\\\\1"' in skill
    assert '\'(0.000 0.250) "A\\"NET\\\\1" "centerCenter" "R0"' in skill


def test_schematic_create_net_stub_rejects_non_positive_length() -> None:
    with pytest.raises(ValueError, match="length must be positive"):
        schematic_create_net_stub("IN", 0, 0, length=0)


def test_schematic_create_net_stub_rejects_unknown_direction() -> None:
    with pytest.raises(ValueError, match="direction must be one of"):
        schematic_create_net_stub("IN", 0, 0, direction="diagonal")

from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.schematic.ops import (
    pin_anchored_label_placement,
    schematic_create_net_stub,
    schematic_create_net_expression,
    schematic_label_instance_term,
    schematic_set_netset_property,
    schematic_wire_instance_terms_labeled,
)


def test_schematic_create_net_expression_requires_wire_on_named_net() -> None:
    skill = schematic_create_net_expression(
        'V"DD',
        '[@vdd:%:"vdd!]',
        1.25,
        -0.5,
        justification="centerLeft",
        rotation="R90",
        font_style="stick",
        height=0.0625,
    )

    assert 'x~>net~>name == "V\\"DD"' in skill
    assert 'unless(rbWire error("wire for net not found"))' in skill
    assert '"[@vdd:%:\\"vdd!]" rbWire \'(1.250 -0.500) "centerLeft" "R90"' in skill


def test_schematic_set_netset_property_escapes_string_literals() -> None:
    skill = schematic_set_netset_property('XI"0', "bulk\\net", 'VDD"TOP')

    assert 'x~>name == "XI\\"0"' in skill
    assert 'unless(rbInst error("instance not found"))' in skill
    assert 'dbReplaceProp(rbInst "bulk\\\\net" "netSet" "VDD\\"TOP")' in skill


@pytest.mark.parametrize(
    ("x", "y", "direction", "length", "wire", "label_xy", "rotation"),
    [
        (0, 0, "right", 0.5, "'((0.000 0.000) (0.500 0.000))", "'(0.250 0.000)", "R0"),
        (1, 2, "up", 0.75, "'((1.000 2.000) (1.000 2.750))", "'(1.000 2.375)", "R90"),
        (0, 0, "left", 1, "'((0.000 0.000) (-1.000 0.000))", "'(-0.500 0.000)", "R0"),
        (0, 0, "down", 1, "'((0.000 0.000) (0.000 -1.000))", "'(0.000 -0.500)", "R90"),
    ],
)
def test_schematic_create_net_stub_places_wire_and_midpoint_label(
    x: float,
    y: float,
    direction: str,
    length: float,
    wire: str,
    label_xy: str,
    rotation: str,
) -> None:
    skill = schematic_create_net_stub(
        "IN", x, y, direction=direction, length=length, label_anchor="midpoint"
    )

    assert wire in skill
    assert f'{label_xy} "IN" "centerCenter" "{rotation}"' in skill


@pytest.mark.parametrize(
    ("direction", "label", "justification", "rotation"),
    [
        # Horizontal: above the wire, reading away from the start point.
        ("right", "'(1.025 2.04375)", "lowerLeft", "R0"),
        ("left", "'(0.975 2.04375)", "lowerRight", "R0"),
        # Vertical: R90 on the left of the wire.
        ("up", "'(0.95625 2.025)", "lowerLeft", "R90"),
        ("down", "'(0.95625 1.975)", "lowerRight", "R90"),
    ],
)
def test_schematic_create_net_stub_labels_the_start_by_default(
    direction: str, label: str, justification: str, rotation: str
) -> None:
    skill = schematic_create_net_stub("IN", 1, 2, direction=direction)

    assert "when(rbWireObj schCreateWireLabel(cv rbWireObj " in skill
    assert f'{label} "IN" "{justification}" "{rotation}"' in skill


def test_default_and_midpoint_net_stub_lengths() -> None:
    default = schematic_create_net_stub("IN", 0, 0)
    midpoint = schematic_create_net_stub("IN", 0, 0, label_anchor="midpoint")
    assert "'((0.000 0.000) (0.250 0.000))" in default
    assert "'((0.000 0.000) (0.500 0.000))" in midpoint


def test_pin_anchored_label_placement_scales_with_height() -> None:
    # Matches the hand-drawn DOP<7> label of a SAR logic chain.
    x, y, just, rot = pin_anchored_label_placement(0.6875, 2.25, "right")
    assert (x, y, just, rot) == (pytest.approx(0.7125), pytest.approx(2.29375), "lowerLeft", "R0")
    x, y, just, rot = pin_anchored_label_placement(0.0, 3.0, "up", height=0.0625)
    assert (x, y, just, rot) == (pytest.approx(-0.04375), pytest.approx(3.025), "lowerLeft", "R90")
    x, y, _, _ = pin_anchored_label_placement(0.0, 0.0, "right", height=0.125)
    assert (x, y) == (pytest.approx(0.05), pytest.approx(0.0875))
    with pytest.raises(ValueError, match="direction must be one of"):
        pin_anchored_label_placement(0, 0, "diagonal")


def test_schematic_create_net_stub_escapes_label_text_and_overrides_rotation() -> None:
    skill = schematic_create_net_stub(
        'A"NET\\1', 0, 0, direction="up", rotation="R0", label_anchor="midpoint"
    )

    assert '\'(0.000 0.250) "A\\"NET\\\\1" "centerCenter" "R0"' in skill


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"length": 0}, "length must be positive"),
        ({"direction": "diagonal"}, "direction must be one of"),
        ({"label_anchor": "end"}, "label_anchor must be"),
    ],
)
def test_schematic_create_net_stub_rejects_invalid_geometry(kwargs, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        schematic_create_net_stub("IN", 0, 0, **kwargs)


@pytest.mark.parametrize(
    ("bind", "cv_expr", "wire_cv", "label_wire"),
    [
        (False, "cv", "cv", "nil"),
        # The stub wire, terminal lookup and label share one cellview.
        (False, "targetCv", "targetCv", "nil"),
        (True, "cv", "cv", "rbWireObj"),
        (True, "targetCv", "targetCv", "rbWireObj"),
    ],
)
def test_schematic_label_instance_term_wire_binding(
    bind: bool,
    cv_expr: str,
    wire_cv: str,
    label_wire: str,
) -> None:
    skill = schematic_label_instance_term(
        "M0",
        "D",
        "OUT",
        cv_expr=cv_expr,
        bind_label_to_wire=bind,
    )

    assert f'schCreateWire({wire_cv} "route" "full"' in skill
    assert f'schCreateWireLabel({cv_expr} {label_wire} rbLblPt "OUT" rbLblJust rbLblRot' in skill
    assert ("rbWireObj" in skill) is bind


def test_label_instance_term_matches_terminal_case_insensitively() -> None:
    # One PDK names MOS terminals D/G/S/B, another d/g/s.
    skill = schematic_label_instance_term("M0", "D", "OUT")

    exact = 'rbFound = car(setof(x rbTerms x~>name == "D"))'
    fallback = 'foreach(rbAlias list("D") unless(rbFound rbFound = car(setof(x rbTerms upperCase(x~>name) == rbAlias))))'
    assert exact in skill and fallback in skill
    assert skill.index(exact) < skill.index(fallback)
    assert 'error("terminal %s not found on %s/%s; its terminals: %L" "D" ' in skill


def test_label_instance_term_tries_aliases_and_can_skip_a_missing_terminal() -> None:
    skill = schematic_label_instance_term("M0", "B", "VSS", aliases=("BULK", "b"), missing_ok=True)

    assert 'list("B" "BULK")' in skill
    assert "not found on" not in skill
    # Every later step is guarded, so a nil terminal draws nothing.
    assert "rbPin = when(rbTerm car(rbTerm~>pins))" in skill
    assert "when(rbWireObj && rbLblPt schCreateWireLabel(" in skill


def test_label_instance_term_default_anchors_label_at_the_terminal() -> None:
    skill = schematic_label_instance_term("M0", "D", "OUT")

    assert 'schCreateWireLabel(cv rbWireObj rbLblPt "OUT"' in skill
    # Placement is computed from the terminal centre toward the stub end.
    assert "rbLblDx = when(rbCtr && rbStubEnd xCoord(rbStubEnd) - xCoord(rbCtr))" in skill
    assert 'rbLblPt = list(xCoord(rbCtr) + 0.025 yCoord(rbCtr) + 0.04375) rbLblJust = "lowerLeft" rbLblRot = "R0"' in skill
    assert 'rbLblPt = list(xCoord(rbCtr) - 0.04375 yCoord(rbCtr) - 0.025) rbLblJust = "lowerRight" rbLblRot = "R90"' in skill
    assert "rbMid \"OUT\"" not in skill


def test_label_instance_term_explicit_style_overrides_default_placement() -> None:
    skill = schematic_label_instance_term("M0", "D", "OUT", justification="centerLeft", rotation="R90")

    assert 'rbLblPt "OUT" "centerLeft" "R90"' in skill


def test_label_instance_term_legacy_preset_keeps_midpoint_label() -> None:
    skill = schematic_label_instance_term("M0", "D", "OUT", cosmetic="legacy")

    assert 'nil rbMid "OUT" "centerCenter" "R0"' in skill
    assert "rbLblPt" not in skill


def test_label_instance_term_rejects_unknown_cosmetic() -> None:
    with pytest.raises(ValueError, match="cosmetic must be one of"):
        schematic_label_instance_term("M0", "D", "OUT", cosmetic="fancy")


def test_wire_instance_terms_labeled_draws_one_wire_and_one_label() -> None:
    skill = schematic_wire_instance_terms_labeled("I3", "ENB_N", "I2", "ENB_P", "ENB_N<5>")

    assert skill.count("schCreateWire(") == 1
    assert skill.count("schCreateWireLabel(") == 1
    assert 'list(rbCtrA rbCtrB)' in skill
    assert "rbLblDx = when(rbCtrA && rbCtrB xCoord(rbCtrB) - xCoord(rbCtrA))" in skill
    assert 'schCreateWireLabel(cv rbWireObj rbLblPt "ENB_N<5>" rbLblJust rbLblRot "stick" 0.0625 nil)' in skill


def _mos_label_commands(**kwargs) -> list[str]:
    from virtuoso_bridge.virtuoso.schematic.editor import SchematicEditor

    editor = SchematicEditor(client=None, lib="L", cell="C")
    editor.add_net_label_to_transistor("M0", **kwargs)
    return editor.commands


def test_mos_labels_require_d_g_s_and_skip_an_absent_body() -> None:
    drain, gate, source, body = _mos_label_commands(
        drain_net="OUT", gate_net="IN", source_net="VSS", body_net="VSS"
    )

    for command, role in ((drain, "D"), (gate, "G"), (source, "S")):
        assert f'error("terminal %s not found on %s/%s; its terminals: %L" "{role}" ' in command
    assert 'list("B" "BULK" "BODY" "SUB")' in body
    assert "not found on" not in body


def test_mos_labels_use_the_terminal_names_given_for_a_role() -> None:
    (body,) = _mos_label_commands(body_net="VSS", terminal_names={"b": "sx"})

    assert 'x~>name == "sx"' in body and 'list("SX")' in body
    assert 'error("terminal %s not found on %s/%s; its terminals: %L" "sx" ' in body

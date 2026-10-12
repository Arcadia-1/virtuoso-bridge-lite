from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_net_stub,
    schematic_create_net_expression,
    schematic_label_instance_term,
    schematic_set_netset_property,
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
    skill = schematic_create_net_stub("IN", x, y, direction=direction, length=length)

    assert wire in skill
    assert f'{label_xy} "IN" "centerCenter" "{rotation}"' in skill


def test_schematic_create_net_stub_escapes_label_text_and_overrides_rotation() -> None:
    skill = schematic_create_net_stub('A"NET\\1', 0, 0, direction="up", rotation="R0")

    assert '\'(0.000 0.250) "A\\"NET\\\\1" "centerCenter" "R0"' in skill


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"length": 0}, "length must be positive"),
        ({"direction": "diagonal"}, "direction must be one of"),
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
    assert f'schCreateWireLabel({cv_expr} {label_wire} rbMid "OUT"' in skill
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
    assert "when(rbMid schCreateWireLabel(" in skill


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

"""Tests for ``virtuoso_bridge.virtuoso.visio`` pin placement (issue #345)."""

from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.visio import build_visio_schematic


@pytest.mark.parametrize(
    "name, cell",
    [
        ("R1", "rpoly"),
        ("C1", "mimcap"),
        ("L1", "spiral_ind"),
        ("V1", "vdc"),
        ("I1", "idc"),
    ],
)
def test_passive_pins_do_not_collapse_when_body_pin_is_first_and_excluded(name, cell) -> None:
    schematic = {
        "instances": [
            {
                "name": name,
                "lib": "pdk",
                "cell": cell,
                "xy": [10.0, 20.0],
                "orient": "R0",
                # "B" is at index 0 and excluded by default; remaining two pins
                # have custom names that fall back to positional placement.
                "terms": {"B": "gnd!", "TOP": "in_net", "BOT": "out_net"},
            }
        ]
    }

    model = build_visio_schematic(schematic)
    inst = model.instances[0]
    assert set(inst.pins) == {"TOP", "BOT"}
    # TOP -> (0.0, +0.5), BOT -> (0.0, -0.5) relative to (10.0, 20.0).
    assert inst.pins["TOP"].x == pytest.approx(10.0)
    assert inst.pins["TOP"].y == pytest.approx(20.0 + 0.5 * inst.height)
    assert inst.pins["BOT"].x == pytest.approx(10.0)
    assert inst.pins["BOT"].y == pytest.approx(20.0 - 0.5 * inst.height)
    assert inst.pins["TOP"].y != inst.pins["BOT"].y


def test_passive_pins_do_not_collapse_when_first_pin_net_is_excluded() -> None:
    schematic = {
        "instances": [
            {
                "name": "R2",
                "lib": "pdk",
                "cell": "res",
                "xy": [0.0, 0.0],
                "orient": "R0",
                "terms": {"SUB": "vss!", "A": "n1", "C": "n2"},
            }
        ]
    }

    model = build_visio_schematic(schematic, exclude_nets=("vss!",))
    inst = model.instances[0]
    assert set(inst.pins) == {"A", "C"}
    assert inst.pins["A"].y == pytest.approx(0.5 * inst.height)
    assert inst.pins["C"].y == pytest.approx(-0.5 * inst.height)


def test_unknown_device_uses_kept_index_after_excluding_body_pin() -> None:
    schematic = {
        "instances": [
            {
                "name": "X1",
                "lib": "custom",
                "cell": "my_block",
                "xy": [0.0, 0.0],
                "orient": "R0",
                "terms": {"B": "gnd!", "IN": "sig_in", "OUT": "sig_out"},
            }
        ]
    }

    model = build_visio_schematic(schematic)
    inst = model.instances[0]
    # UNKNOWN_DEVICE pins[0] is (0.0, +0.5), pins[1] is (0.0, -0.5).
    assert inst.pins["IN"].x == pytest.approx(0.0)
    assert inst.pins["IN"].y == pytest.approx(0.5 * inst.height)
    assert inst.pins["OUT"].x == pytest.approx(0.0)
    assert inst.pins["OUT"].y == pytest.approx(-0.5 * inst.height)

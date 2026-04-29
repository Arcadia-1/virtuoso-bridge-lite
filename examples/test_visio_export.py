"""Smoke-check the pure Visio export model builder.

This example does not launch Microsoft Visio; it only exercises the
dependency-free conversion layer that turns `read_schematic()`-style data into
placed instances and routed net segments.
"""

from virtuoso_bridge.virtuoso.visio import (
    build_visio_schematic,
    classify_instance,
    minimum_spanning_segments,
)


def main() -> int:
    spec = classify_instance({"name": "M0", "cell": "pch_mac"})
    assert spec.device_type == "PMOS"

    schematic = {
        "instances": [
            {
                "name": "M0",
                "lib": "analogLib",
                "cell": "nch",
                "xy": [1.0, 2.0],
                "orient": "R0",
                "terms": {"D": "out", "G": "in", "S": "vss", "B": "vss"},
            },
            {
                "name": "R0",
                "lib": "analogLib",
                "cell": "res",
                "xy": [1.0, 3.0],
                "orient": "R0",
                "terms": {"PLUS": "out", "MINUS": "vdd"},
            },
            {
                "name": "L0",
                "lib": "analogLib",
                "cell": "ind",
                "xy": [1.5, 3.0],
                "orient": "R0",
                "terms": {"PLUS": "vdd", "MINUS": "out"},
            },
            {
                "name": "V0",
                "lib": "analogLib",
                "cell": "vdc",
                "xy": [2.0, 3.0],
                "orient": "R0",
                "terms": {"PLUS": "vdd", "MINUS": "vss"},
            },
            {
                "name": "I0",
                "lib": "analogLib",
                "cell": "idc",
                "xy": [2.5, 3.0],
                "orient": "R0",
                "terms": {"PLUS": "out", "MINUS": "vss"},
            },
        ],
    }

    model = build_visio_schematic(schematic)
    assert len(model.instances) == 5
    assert model.instances[0].device_type == "NMOS"
    assert model.instances[2].master_name == "L"
    assert model.instances[3].master_name == "DC-V"
    assert model.instances[4].master_name == "DC-I"
    assert "B" not in model.instances[0].pins
    assert "out" in model.nets
    assert len(model.nets["out"].pins) >= 2
    assert len(model.nets["out"].segments) == len(model.nets["out"].pins) - 1

    excluded = build_visio_schematic(schematic, exclude_nets={"VDD", "VSS"})
    assert "vdd" not in {name.lower() for name in excluded.nets}
    assert "vss" not in {name.lower() for name in excluded.nets}

    points = [(0.0, 0.0), (1.0, 0.0), (4.0, 0.0), (4.0, 3.0)]
    assert len(minimum_spanning_segments(points)) == len(points) - 1

    print("visio export smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

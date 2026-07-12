#!/usr/bin/env python3
"""Draw a symbol with native pin, instance, and logical label semantics."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.symbol import (
    symbol_create_instance_label,
    symbol_create_logical_label,
    symbol_create_pin,
    symbol_create_polygon,
    symbol_create_selection_box,
    symbol_set_term_order,
)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: python {Path(__file__).name} <LIB>", file=sys.stderr)
        return 1

    lib = sys.argv[1]
    cell = f"VB_MANUAL_SYMBOL_{datetime.now():%Y%m%d_%H%M%S}"
    client = VirtuosoClient.from_env()

    with client.symbol.edit(lib, cell) as symbol:
        symbol.add(
            symbol_create_polygon(
                "device",
                "drawing",
                [(-1.0, -0.75), (-1.0, 0.75), (1.0, 0.0)],
            )
        )
        symbol.add(
            symbol_create_pin(
                "VIN",
                -1.5,
                0.25,
                direction="input",
                label_x=-0.9,
                label_y=0.25,
            )
        )
        symbol.add(
            symbol_create_pin(
                "VOUT",
                1.5,
                0.0,
                direction="output",
                label_x=0.9,
                label_y=0.0,
                label_justification="centerRight",
            )
        )
        symbol.add(symbol_create_instance_label(0.0, 1.0))
        symbol.add(symbol_create_logical_label(0.0, -1.0))
        symbol.add(symbol_create_selection_box(-1.5, -0.75, 1.5, 0.75))
        symbol.add(symbol_set_term_order(["VIN", "VOUT"]))

    ports = client.symbol.read_ports(lib, cell)
    labels = {label["text"]: label for label in ports["labels"]}
    assert labels["VIN"]["layerName"] == "pin"
    assert labels["VIN"]["purpose"] == "label"
    assert labels["[@instanceName]"]["labelType"] == "NLPLabel"
    assert labels["[@instanceName]"]["layerName"] == "instance"
    assert labels["[@partName]"]["labelType"] == "NLPLabel"
    assert labels["[@partName]"]["layerName"] == "device"
    assert ports["selectionBoxes"] == [[[-1.5, -0.75], [1.5, 0.75]]]

    client.open_window(lib, cell, view="symbol")
    print(f"Created and verified {lib}/{cell}/symbol")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

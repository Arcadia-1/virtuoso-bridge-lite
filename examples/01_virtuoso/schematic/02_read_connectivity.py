#!/usr/bin/env python3
"""Read full connectivity from a schematic.

Usage::

    python 02_read_connectivity.py MYLIB MYCELL
    python 02_read_connectivity.py              # uses the active design
"""

from __future__ import annotations

import sys

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.reader import read_connectivity


def format_connectivity(data: dict) -> str:
    lines: list[str] = []
    instances = data.get("instances", [])
    nets = data.get("nets", [])
    pins = data.get("pins", [])

    lines.append(f"Instances : {len(instances)}   Nets : {len(nets)}   Pins : {len(pins)}")

    if instances:
        name_w = max(len(i["name"]) for i in instances)
        lines.append(f"\n{'INSTANCE':<{name_w}}  LIB/CELL")
        lines.append("-" * (name_w + 30))
        for i in instances:
            lines.append(f"{i['name']:<{name_w}}  {i['lib']}/{i['cell']}")

    if nets:
        net_w = max(len(n["name"]) for n in nets)
        lines.append(f"\n{'NET':<{net_w}}  CONNECTIONS (inst.terminal)")
        lines.append("-" * (net_w + 50))
        for n in nets:
            lines.append(f"{n['name']:<{net_w}}  {'  '.join(n['connections'])}")

    if pins:
        pin_w = max(len(p["name"]) for p in pins)
        lines.append(f"\n{'PIN':<{pin_w}}  DIRECTION")
        lines.append("-" * (pin_w + 20))
        for p in pins:
            lines.append(f"{p['name']:<{pin_w}}  {p['direction']}")

    return "\n".join(lines)


def main() -> int:
    client = VirtuosoClient.from_env()

    if len(sys.argv) >= 3:
        lib, cell = sys.argv[1], sys.argv[2]
    else:
        lib, cell, _ = client.get_current_design()
        if not lib:
            print("Usage: python 02_read_connectivity.py LIB CELL")
            print("       or open a schematic in Virtuoso first.")
            return 1

    print(f"Reading {lib}/{cell}/schematic ...")
    data = read_connectivity(client, lib, cell)
    print(format_connectivity(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

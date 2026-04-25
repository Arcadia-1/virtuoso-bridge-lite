#!/usr/bin/env python3
"""Demonstrate the unified read_schematic() API.

read_schematic() returns topology, positions, CDF params, and notes in a
single SKILL call — superseding the legacy read_connectivity /
read_instance_params / read_placement functions.

Usage::

    python 11_read_schematic_unified.py LIB CELL            # full read
    python 11_read_schematic_unified.py LIB CELL --no-pos   # topology only
    python 11_read_schematic_unified.py                     # active schematic
    python 11_read_schematic_unified.py LIB CELL --all-params

Options:
    --no-pos       Omit xy/orient/bBox (faster for large schematics)
    --all-params   Return all CDF params without filtering
"""

from __future__ import annotations

import sys

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.reader import read_schematic


def _print_result(data: dict, *, include_positions: bool) -> None:
    instances = data.get("instances", [])
    nets = data.get("nets", {})
    pins = data.get("pins", {})
    notes = data.get("notes", [])

    print(f"Instances : {len(instances)}   Nets : {len(nets)}   "
          f"Pins : {len(pins)}   Notes : {len(notes)}")

    if instances:
        name_w = max(len(i["name"]) for i in instances)
        print(f"\n{'INSTANCE':<{name_w}}  LIB/CELL           TERMS")
        print("-" * (name_w + 50))
        for i in instances:
            terms = "  ".join(f"{t}={n}" for t, n in i.get("terms", {}).items())
            line = f"{i['name']:<{name_w}}  {i['lib']}/{i['cell']}"
            if include_positions:
                xy = i.get("xy", [0, 0])
                line += f"  @({xy[0]:.1f},{xy[1]:.1f}) {i.get('orient', '?')}"
            if terms:
                line += f"  {terms}"
            print(line)

            params = i.get("params", {})
            if params:
                param_str = "  ".join(f"{k}={v}" for k, v in params.items())
                print(f"{'':<{name_w}}  params: {param_str}")

            nl = i.get("nlAction")
            if nl:
                print(f"{'':<{name_w}}  nlAction={nl}")

    if nets:
        net_w = max(len(n) for n in nets)
        print(f"\n{'NET':<{net_w}}  BITS  TYPE     CONNECTIONS")
        print("-" * (net_w + 50))
        for name, n in nets.items():
            conns = "  ".join(n.get("connections", []))
            print(f"{name:<{net_w}}  {n.get('numBits', 1):<5} {n.get('sigType', '?'):<9} {conns}")

    if pins:
        pin_w = max(len(p) for p in pins)
        print(f"\n{'PIN':<{pin_w}}  DIR           BITS")
        print("-" * (pin_w + 20))
        for name, p in pins.items():
            print(f"{name:<{pin_w}}  {p['direction']:<14}{p.get('numBits', 1)}")

    if notes:
        print(f"\nNOTES ({len(notes)}):")
        for n in notes:
            print(f"  \"{n['text']}\"")


def main() -> int:
    argv = sys.argv[1:]
    include_positions = "--no-pos" not in argv
    all_params = "--all-params" in argv
    argv = [a for a in argv if a not in ("--no-pos", "--all-params")]

    lib = argv[0] if len(argv) >= 1 else None
    cell = argv[1] if len(argv) >= 2 else None

    client = VirtuosoClient.from_env()

    if not lib or not cell:
        lib, cell, _ = client.get_current_design()
        if not lib:
            print("Usage: python 11_read_schematic_unified.py LIB CELL [--no-pos] [--all-params]")
            print("       or open a schematic in Virtuoso first.")
            return 1

    print(f"Reading {lib}/{cell}/schematic (positions={'on' if include_positions else 'off'}, "
          f"params={'all' if all_params else 'filtered'}) ...")

    kw = {"include_positions": include_positions}
    if all_params:
        kw["param_filters"] = None

    data = read_schematic(client, lib, cell, **kw)

    _print_result(data, include_positions=include_positions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

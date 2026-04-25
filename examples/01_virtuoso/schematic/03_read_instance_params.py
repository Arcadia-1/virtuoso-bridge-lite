#!/usr/bin/env python3
"""Dump CDF instance parameters from a schematic.

Usage::

    python 03_read_instance_params.py                        # active schematic, all params
    python 03_read_instance_params.py LIB CELL              # specific cell, all params
    python 03_read_instance_params.py --filter w l nf m     # restrict to named params
"""

from __future__ import annotations

import sys

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.reader import read_instance_params


def main() -> int:
    argv = sys.argv[1:]

    filter_names = None
    if "--filter" in argv:
        idx = argv.index("--filter")
        filter_names = argv[idx + 1:]
        argv = argv[:idx]

    lib = argv[0] if len(argv) >= 1 else None
    cell = argv[1] if len(argv) >= 2 else None

    client = VirtuosoClient.from_env()

    if not lib or not cell:
        lib, cell, _ = client.get_current_design()
        if not lib:
            print("Usage: python 03_read_instance_params.py LIB CELL [--filter w l nf m]")
            print("       or open a schematic in Virtuoso first.")
            return 1

    print(f"Reading {lib}/{cell}/schematic ...")
    params = read_instance_params(client, lib, cell, filter_params=filter_names)

    for inst in params:
        if inst["params"]:
            print(f"\n{inst['name']}  [{inst['lib']}/{inst['cell']}]")
            for k, v in inst["params"].items():
                print(f"  {k} = {v}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

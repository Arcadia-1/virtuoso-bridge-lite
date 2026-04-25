#!/usr/bin/env python3
"""Create analogLib instances and verify set_instance_params() writes values.

Usage::

    python 04_test_set_instance_params_analoglib.py <LIB>
    python 04_test_set_instance_params_analoglib.py <LIB> <CELL>

This script will:
1. Create a schematic with common analogLib components (vdc, idc, res, cap, ind)
2. Apply CDF values via set_instance_params(..., strict=True)
3. Read back params and assert they match expected values
"""

from __future__ import annotations

import sys
from datetime import datetime

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_inst_by_master_name as inst,
)
from virtuoso_bridge.virtuoso.schematic.params import set_instance_params
from virtuoso_bridge.virtuoso.schematic.reader import read_schematic

# Local test environment record
TEST_CONDA_ENV = "vector"
TEST_PDK = "tsmcN65"


EXPECTED_PARAMS: dict[str, dict[str, str]] = {
    "I0": {"idc": "100u"},
    "R0": {"r": "10k"},
    "C0": {"c": "1p"},
    "L0": {"l": "10n", "r": "1"},
}


def _normalize(value: str) -> str:
    return str(value).strip().strip('"').lower()


def _create_schematic(client: VirtuosoClient, lib: str, cell: str) -> None:
    with client.schematic.edit(lib, cell) as sch:
        sch.add(inst("analogLib", "idc", "symbol", "I0", 0.0, 2.0, "R0"))
        sch.add(inst("analogLib", "res", "symbol", "R0", 3.0, 2.0, "R0"))
        sch.add(inst("analogLib", "cap", "symbol", "C0", 0.0, 0.0, "R0"))
        sch.add(inst("analogLib", "ind", "symbol", "L0", 3.0, 0.0, "R0"))


def _set_params(client: VirtuosoClient) -> None:
    set_instance_params(client, "I0", strict=True, idc=EXPECTED_PARAMS["I0"]["idc"])
    set_instance_params(client, "R0", strict=True, r=EXPECTED_PARAMS["R0"]["r"])
    set_instance_params(client, "C0", strict=True, c=EXPECTED_PARAMS["C0"]["c"])
    set_instance_params(
        client,
        "L0",
        strict=True,
        l=EXPECTED_PARAMS["L0"]["l"],
        r=EXPECTED_PARAMS["L0"]["r"],
    )


def _verify_params(client: VirtuosoClient, lib: str, cell: str) -> None:
    data = read_schematic(client, lib, cell, include_positions=False, param_filters=None)
    by_name = {inst_data["name"]: inst_data for inst_data in data["instances"]}

    for inst_name, expected in EXPECTED_PARAMS.items():
        if inst_name not in by_name:
            raise AssertionError(f"Missing instance in schematic readback: {inst_name}")

        actual_params = by_name[inst_name].get("params", {})
        for key, value in expected.items():
            actual_value = actual_params.get(key)
            if actual_value is None:
                raise AssertionError(f"{inst_name}: missing param '{key}' in readback")
            if _normalize(actual_value) != _normalize(value):
                raise AssertionError(
                    f"{inst_name}: param '{key}' mismatch, expected '{value}', got '{actual_value}'"
                )


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <LIB> [CELL]", file=sys.stderr)
        return 1

    lib = sys.argv[1]
    cell = (
        sys.argv[2]
        if len(sys.argv) >= 3
        else f"test_set_instance_params_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )

    client = VirtuosoClient.from_env()

    print(f"Recorded env: conda={TEST_CONDA_ENV}, PDK={TEST_PDK}")
    print(f"[1/4] Creating schematic: {lib}/{cell}/schematic")
    _create_schematic(client, lib, cell)

    print("[2/4] Opening schematic to make it active")
    client.open_window(lib, cell, view="schematic")

    print("[3/4] Setting CDF params with set_instance_params(..., strict=True)")
    _set_params(client)

    print("[4/4] Verifying readback")
    _verify_params(client, lib, cell)

    print("PASS: set_instance_params works for common analogLib components")
    print(f"Schematic: {lib}/{cell}/schematic")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

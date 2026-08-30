"""Create and verify a differential-pair schematic from explicit constraints.

Required environment:
    VB_EXAMPLE_LIB   existing writable target library
    VB_PDK_LIB       library containing the MOS symbols

Optional environment:
    VB_NCH_CELL      NMOS cell name (default: nch)
    VB_PCH_CELL      PMOS cell name (default: pch)
"""

from __future__ import annotations

import os

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic import (
    DeviceKind,
    DifferentialPairConstraint,
    GridPositionConstraint,
    SchematicInstanceSpec,
    SchematicPinSpec,
    SchematicPlanRequest,
)


TARGET_LIB = os.environ["VB_EXAMPLE_LIB"]
PDK_LIB = os.environ["VB_PDK_LIB"]
NCH = os.environ.get("VB_NCH_CELL", "nch")
PCH = os.environ.get("VB_PCH_CELL", "pch")
TARGET_CELL = "vb_planned_diff_pair"


def mos(
    name: str,
    cell: str,
    kind: DeviceKind,
    drain: str,
    gate: str,
    source: str,
    body: str,
    *,
    output_stage: bool = False,
) -> SchematicInstanceSpec:
    return SchematicInstanceSpec(
        name,
        PDK_LIB,
        cell,
        kind=kind,
        output_stage=output_stage,
        terminals={"D": drain, "G": gate, "S": source, "B": body},
    )


request = SchematicPlanRequest(
    instances=[
        mos("M_TAIL", NCH, DeviceKind.NMOS, "VS", "BIAS", "VSS", "VSS"),
        mos("M_INP", NCH, DeviceKind.NMOS, "VOP", "VINP", "VS", "VSS"),
        mos("M_INN", NCH, DeviceKind.NMOS, "VON", "VINN", "VS", "VSS"),
        mos("M_LOADP", PCH, DeviceKind.PMOS, "VOP", "VON", "VDD", "VDD"),
        mos("M_LOADN", PCH, DeviceKind.PMOS, "VON", "VON", "VDD", "VDD"),
        mos(
            "M_OUT",
            NCH,
            DeviceKind.NMOS,
            "OUT",
            "VOP",
            "VSS",
            "VSS",
            output_stage=True,
        ),
    ],
    pins=[
        SchematicPinSpec("VINP", "input", row=1),
        SchematicPinSpec("VINN", "input", row=2),
        SchematicPinSpec("OUT", "output", row=0),
    ],
    positions=[GridPositionConstraint("M_TAIL", col=1.5, row=0)],
    differential_pairs=[
        DifferentialPairConstraint("M_INP", "M_INN", row=1, center_col=1.5),
        DifferentialPairConstraint("M_LOADP", "M_LOADN", row=2, center_col=1.5),
    ],
)

client = VirtuosoClient.from_env()
plan = client.schematic.plan(request)
for diagnostic in plan.diagnostics:
    print(f"[{diagnostic.level.value}] {diagnostic.message}")

client.schematic.create_from_plan(TARGET_LIB, TARGET_CELL, plan)
readback = client.schematic.read(TARGET_LIB, TARGET_CELL, include_positions=True)
plan.verify_readback(readback).require_valid()
print(f"Created and verified {TARGET_LIB}/{TARGET_CELL}/schematic")

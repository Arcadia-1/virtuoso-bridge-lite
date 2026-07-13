"""MOS operating-point diagnostics."""

from __future__ import annotations

import math
from typing import Any


class DiagnosticError(ValueError):
    """Raised when operating-point data is incomplete or nonphysical."""


_REQUIRED = ("region", "gm", "gds", "vds", "vdsat", "id")


def diagnose_mos_operating_points(points: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not points:
        raise DiagnosticError("MOS operating point data is missing")
    result: dict[str, dict[str, Any]] = {}
    for instance, values in points.items():
        missing = [name for name in _REQUIRED if name not in values]
        if missing:
            raise DiagnosticError(f"{instance} operating point is missing: {', '.join(missing)}")
        numeric = {name: float(values[name]) for name in ("gm", "gds", "vds", "vdsat", "id")}
        if not all(math.isfinite(value) for value in numeric.values()):
            raise DiagnosticError(f"{instance} operating point values must be finite")
        if numeric["id"] == 0 or numeric["gds"] == 0:
            raise DiagnosticError(f"{instance} current and gds must be nonzero")
        result[instance] = {
            "region": str(values["region"]),
            "gm_over_id": abs(numeric["gm"] / numeric["id"]),
            "intrinsic_gain": abs(numeric["gm"] / numeric["gds"]),
            "saturation_margin": abs(numeric["vds"]) - abs(numeric["vdsat"]),
            **numeric,
        }
    return result

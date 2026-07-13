"""
SMIC180 Spec Checker -- Automated pass/fail verification.

Uses bridge-lite's read_results() output which already contains per-output
``pass_fail`` fields set by Maestro's built-in spec evaluator.

This module adds SMIC180-specific analysis on top:
  - Per-pin violation summary
  - Severity classification (critical / major / minor)
  - JSON + text report output

Core principle: does NOT re-implement spec evaluation.  Maestro already
evaluates specs; this module just aggregates and reports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class SpecViolation:
    """A single spec violation from Maestro results."""
    point: int
    output_name: str
    value: str
    spec: str
    severity: str = "major"   # critical / major / minor

    @property
    def is_critical(self) -> bool:
        return self.severity == "critical"


@dataclass
class SpecCheckResult:
    """Aggregated spec check result."""
    overall: str | None = None          # "passed" / "failed" / None
    total_outputs: int = 0
    passed_outputs: int = 0
    failed_outputs: int = 0
    violations: list[SpecViolation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.overall == "passed" and self.failed_outputs == 0

    def summary(self) -> dict:
        return {
            "overall": self.overall,
            "passed": self.passed,
            "total_outputs": self.total_outputs,
            "passed_outputs": self.passed_outputs,
            "failed_outputs": self.failed_outputs,
            "violations": [
                {
                    "point": v.point,
                    "output": v.output_name,
                    "value": v.value,
                    "spec": v.spec,
                    "severity": v.severity,
                }
                for v in self.violations
            ],
        }

    def save(self, run_dir: Path) -> Path:
        path = run_dir / "spec_check.json"
        path.write_text(
            json.dumps(self.summary(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[spec-check] Saved: {path}")
        return path

    def report_text(self) -> str:
        """Human-readable text report."""
        lines = []
        lines.append("=== SMIC180 Spec Check Report ===")
        lines.append(f"Overall: {self.overall or 'unknown'}")
        lines.append(f"Passed:  {self.passed_outputs}/{self.total_outputs}")
        lines.append(f"Failed:  {self.failed_outputs}/{self.total_outputs}")
        lines.append("")
        if self.violations:
            lines.append("Violations:")
            for v in self.violations:
                lines.append(
                    f"  [{v.severity.upper()}] point={v.point}  "
                    f"{v.output_name}: value={v.value}  spec={v.spec}"
                )
        else:
            lines.append("No violations.")
        return "\n".join(lines)


def _classify_severity(output_name: str, spec: str, value: str) -> str:
    """Classify violation severity based on output name and spec type.

    Rules:
      - critical: power pin current (I_*) exceeded max -> potential damage
      - major:    voltage spec (vmax/vmin) failed -> functional failure
      - minor:    everything else (custom expressions, margins)
    """
    name_lower = output_name.lower()

    # Power current violations are critical
    if name_lower.startswith("i_"):
        return "critical"

    # Voltage min/max violations are major
    if name_lower.startswith("vmax_") or name_lower.startswith("vmin_"):
        return "major"

    return "minor"


def check_maestro_specs(read_result: dict) -> SpecCheckResult:
    """Check specs from bridge-lite read_results() output.

    Parameters
    ----------
    read_result : dict
        Output from ``virtuoso_bridge.virtuoso.maestro.reader.runs.read_results()``.
        Must contain ``"points"`` and ``"overall_spec"`` keys.

    Returns
    -------
    SpecCheckResult
        Aggregated pass/fail result with violation details.
    """
    result = SpecCheckResult()
    result.overall = read_result.get("overall_spec")

    total = 0
    passed = 0
    failed = 0

    for pt in read_result.get("points", []):
        point_num = pt.get("point", 0)
        for name, info in (pt.get("outputs") or {}).items():
            if not isinstance(info, dict):
                continue
            pf = (info.get("pass_fail") or "").strip().lower()
            if not pf:
                continue  # no spec set for this output

            total += 1
            if pf == "passed":
                passed += 1
            elif pf == "failed":
                failed += 1
                severity = _classify_severity(
                    name, info.get("spec", ""), info.get("value", "")
                )
                result.violations.append(SpecViolation(
                    point=point_num,
                    output_name=name,
                    value=info.get("value", ""),
                    spec=info.get("spec", ""),
                    severity=severity,
                ))

    result.total_outputs = total
    result.passed_outputs = passed
    result.failed_outputs = failed
    return result


def check_pin_measurements(
    measurements: dict,
    *,
    i_max: float = 0.1,
    vdd: float = 1.8,
) -> SpecCheckResult:
    """Check specs from sim_io/maestro/results.py parse_maestro_measurements() output.

    This is a fallback for when read_results() is not available.
    Applies basic SMIC180 IO ring rules:
      - Power pin current < i_max (default 100mA)
      - Digital pin vmax > 0.9*VDD, vmin < 0.1*VDD
    """
    result = SpecCheckResult()
    result.overall = "passed"
    total = 0
    passed = 0

    pins = measurements.get("pins", {})
    for pin_name, m in pins.items():
        pad_type = m.get("pad_type", "")

        # Power pin current check
        if pad_type == "power":
            iavg = m.get("iavg")
            if iavg is not None:
                total += 1
                if abs(iavg) > i_max:
                    result.violations.append(SpecViolation(
                        point=1,
                        output_name=f"I_{pin_name}",
                        value=f"{iavg:.6g}",
                        spec=f"< {i_max}",
                        severity="critical",
                    ))
                    result.overall = "failed"
                else:
                    passed += 1

        # Digital pin voltage checks
        if pad_type in ("digital_input", "digital_output", "digital_bidirectional",
                        "clock", "reset"):
            vmax = m.get("vmax")
            vmin = m.get("vmin")
            vmin_limit = 0.1 * vdd
            vmax_limit = 0.9 * vdd

            if vmax is not None:
                total += 1
                if vmax < vmax_limit:
                    result.violations.append(SpecViolation(
                        point=1,
                        output_name=f"vmax_{pin_name}",
                        value=f"{vmax:.4g}",
                        spec=f"> {vmax_limit:.4g}",
                        severity="major",
                    ))
                    result.overall = "failed"
                else:
                    passed += 1

            if vmin is not None:
                total += 1
                if vmin > vmin_limit:
                    result.violations.append(SpecViolation(
                        point=1,
                        output_name=f"vmin_{pin_name}",
                        value=f"{vmin:.4g}",
                        spec=f"< {vmin_limit:.4g}",
                        severity="major",
                    ))
                    result.overall = "failed"
                else:
                    passed += 1

    result.total_outputs = total
    result.passed_outputs = passed
    result.failed_outputs = total - passed
    return result

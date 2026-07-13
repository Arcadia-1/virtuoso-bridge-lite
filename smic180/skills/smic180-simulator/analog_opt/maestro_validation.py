"""Strict post-publication Maestro delivery contract."""
from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Mapping

from analog_opt.final_validation import FinalValidationError, PublishedContext, load_published_context


class MaestroValidationError(FinalValidationError):
    """Raised when a Maestro delivery cannot be trusted."""


@dataclass(frozen=True)
class MaestroCorner:
    name: str
    process: str
    voltage: float
    temperature: float


@dataclass(frozen=True)
class MaestroContext:
    published: PublishedContext
    final_testbench: str
    maestro_testbench: str
    maestro_cell: str
    test_name: str
    corners: tuple[MaestroCorner, ...]
    model_file: str
    voltage_variable: str

    @property
    def run_dir(self) -> Path:
        return self.published.run_dir


def _corner_number(value: float, *, temperature: bool = False) -> str:
    number = float(value)
    if temperature:
        return ("N" if number < 0 else "P") + str(abs(int(number)) if number.is_integer() else abs(number)).replace(".", "P")
    return str(number).replace(".", "P")


def _model_file(config: Mapping[str, Any]) -> str:
    models = config.get("models", config.get("model_includes", ()))
    if isinstance(models, Mapping):
        models = models.get("includes", ())
    for item in models or ():
        if isinstance(item, str) and item:
            return item
        if isinstance(item, Mapping) and isinstance(item.get("path"), str) and item["path"]:
            return item["path"]
    return "/home/IC/Tech/smic18ee_2P6M_20100810/models/spectre/e2r018_v1p8_spe.scs"


def load_maestro_context(run_dir: str | Path) -> MaestroContext:
    published = load_published_context(run_dir)
    batch = published.run_dir / "final_validation" / "final_validation.confirmed.json"
    try:
        confirmation = json.loads(batch.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaestroValidationError("batch Spectre final validation confirmation is required") from exc
    if confirmation.get("status") != "passed":
        raise MaestroValidationError("batch Spectre final validation did not pass")
    pvt = published.config.get("pvt")
    if not isinstance(pvt, Mapping):
        raise MaestroValidationError("resolved PVT configuration is missing")
    processes = tuple(str(value).upper() for value in pvt.get("corners", ()))
    voltages = tuple(float(value) for value in pvt.get("voltages", ()))
    temperatures = tuple(float(value) for value in pvt.get("temperatures_c", pvt.get("temperatures", ())))
    if not processes or not voltages or not temperatures:
        raise MaestroValidationError("explicit process, voltage, and temperature PVT values are required")
    corners = tuple(
        MaestroCorner(f"{process}_V{_corner_number(voltage)}_T{_corner_number(temperature, temperature=True)}",
                      process, voltage, temperature)
        for process, voltage, temperature in product(processes, voltages, temperatures)
    )
    names = tuple(item.name for item in corners)
    if len(names) != len(set(names)):
        raise MaestroValidationError("Maestro corner names are not unique")
    result = published.result_cell
    final_tb = result + "_tb"
    maestro_tb = result + "_maestro_tb"
    maestro_cell = result + "_maestro"
    identifiers = {published.source_cell, published.work_cell, result, final_tb, maestro_tb, maestro_cell}
    if len(identifiers) != 6:
        raise MaestroValidationError("Maestro delivery identifiers are not isolated")
    return MaestroContext(published, final_tb, maestro_tb, maestro_cell, "amp_op_ac", corners,
                          _model_file(published.config), str(pvt.get("voltage_stimulus", "VDD")))


def write_maestro_confirmation(run_dir: str | Path, checks: Mapping[str, Any], details: Mapping[str, Any]) -> Path:
    required_true = ("maestro_cell_exists", "maestro_testbench_exists", "test_exists",
                     "dut_uses_result_cell", "model_sections_verified", "maestro_run_completed",
                     "history_exists", "reopen_check_passed")
    if any(checks.get(name) is not True for name in required_true):
        raise MaestroValidationError("Maestro validation checks are incomplete")
    if checks.get("corner_count") != 45 or checks.get("failed_corner_count") != 0:
        raise MaestroValidationError("Maestro validation requires 45 passing corners")
    target = Path(run_dir) / "maestro_validation" / "maestro_validation.confirmed.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "status": "passed", "checks": dict(checks), "details": dict(details)}
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def _quantity(value: str) -> float:
    text = str(value).strip()
    suffix = {"G": 1e9, "M": 1e6, "K": 1e3, "k": 1e3, "m": 1e-3, "u": 1e-6, "n": 1e-9}
    if text and text[-1] in suffix:
        return float(text[:-1]) * suffix[text[-1]]
    return float(text)


def verify_maestro_netlist(text: str, result_cell: str, voltage_variable: str) -> dict[str, float]:
    import re
    if result_cell not in text:
        raise MaestroValidationError("Maestro netlist does not reference result cell")
    required = {
        "SRC_AVD": (r"\bvsource\b", rf"\bdc\s*=\s*{re.escape(voltage_variable)}\b", r"\btype\s*=\s*dc\b"),
        "PVSS_AVS": (r"\bvsource\b", r"\bdc\s*=\s*0(?:\.0+)?\b", r"\btype\s*=\s*dc\b"),
        "SRC_VIN": (r"\bvsource\b", r"\bdc\s*=\s*(?:750(?:\.00m|m)|0\.75|0\.750|750e-3)\b", r"\btype\s*=\s*dc\b"),
        "SRC_VIP": (r"\bvsource\b", r"\bdc\s*=\s*(?:750(?:\.00m|m)|0\.75|0\.750|750e-3)\b", r"\btype\s*=\s*dc\b", r"\bmag\s*=\s*1\b"),
        "SRC_IBIAS": (r"\bisource\b", r"\bdc\s*=\s*10u\b", r"\btype\s*=\s*dc\b"),
        "LOAD_VOUT": (r"\bcapacitor\b", r"\bc\s*=\s*1p\b"),
    }
    values = {}
    for instance, patterns in required.items():
        match = re.search(rf"(?mi)^\s*{re.escape(instance)}\b[^\n]*$", text)
        if not match or any(not re.search(pattern, match.group(0), re.I) for pattern in patterns):
            raise MaestroValidationError(f"Maestro netlist stimulus mismatch: {instance}")
        if instance == "SRC_IBIAS":
            values[instance] = round(_quantity(re.search(r"\bdc\s*=\s*([^\s]+)", match.group(0), re.I).group(1)), 12)
    return values


def build_corner_status(point: Mapping[str, Any], *, spectre_completed: bool, spectre_errors: int) -> dict[str, Any]:
    if not spectre_completed:
        category = "spectre_incomplete"
    elif spectre_errors:
        category = "spectre_error"
    elif any(str(item.get("pass_fail", "")).lower() in {"no", "fail", "failed"}
             for item in (point.get("outputs") or {}).values() if isinstance(item, Mapping)):
        category = "spec_fail"
    elif any(str(item.get("value", "")).lower() in {"error", "nan", "none", ""}
             for item in (point.get("outputs") or {}).values() if isinstance(item, Mapping)):
        category = "output_error"
    else:
        category = "none"
    return {"corner": point.get("corner"), "outputs": dict(point.get("outputs") or {}),
            "spectre_completed": bool(spectre_completed), "spectre_errors": int(spectre_errors),
            "failure_category": category, "passed": category == "none"}
"""Strict post-publication Maestro delivery contract."""
from __future__ import annotations

import json
import math
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
class MaestroProfileContext:
    profile_id: str
    role: str
    final_testbench: str
    maestro_testbench: str
    test_name: str
    dut_instance: str
    analysis_types: tuple[str, ...]
    stimuli: Mapping[str, Mapping[str, Any]]
    analyses: tuple[Mapping[str, Any], ...]
    metrics: tuple[Mapping[str, Any], ...]
    specs: tuple[Mapping[str, Any], ...]
    expected_corner_count: int


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
    profiles: tuple[MaestroProfileContext, ...] = ()

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
    maestro_cell = result + "_maestro"
    raw_profiles = published.config.get("verification_profiles")
    profiles = []
    if isinstance(raw_profiles, (list, tuple)) and raw_profiles and not (len(raw_profiles) == 1 and isinstance(raw_profiles[0], Mapping) and raw_profiles[0].get("id") == "default" and raw_profiles[0].get("role") == "legacy"):
        if not isinstance(published.profile_summary_hash, str) or len(published.profile_summary_hash) != 64:
            raise MaestroValidationError("multi-profile Maestro validation requires publication profile summary hash")
        details = confirmation.get("details")
        profile_details = details.get("profiles") if isinstance(details, Mapping) else None
        required_ids = details.get("required_profile_ids") if isinstance(details, Mapping) else None
        configured_ids = [item.get("id") for item in raw_profiles if isinstance(item, Mapping)]
        if required_ids != configured_ids or not isinstance(profile_details, Mapping):
            raise MaestroValidationError("batch Spectre profile confirmation does not match configuration")
        if published.profile_summary_hash is not None and details.get("profile_summary_hash") != published.profile_summary_hash:
            raise MaestroValidationError("batch Spectre profile summary hash does not match publication")
        for raw in raw_profiles:
            profile_id = raw.get("id") if isinstance(raw, Mapping) else None
            role = raw.get("role") if isinstance(raw, Mapping) else None
            dut_instance = raw.get("dut_instance") if isinstance(raw, Mapping) else None
            info = profile_details.get(profile_id) if isinstance(profile_id, str) else None
            final_testbench = info.get("final_testbench") if isinstance(info, Mapping) else None
            expected_corner_count = info.get("pvt_point_count") if isinstance(info, Mapping) else None
            analyses = raw.get("analyses") if isinstance(raw, Mapping) else None
            stimuli = raw.get("stimuli") if isinstance(raw, Mapping) else None
            metrics = raw.get("metrics", ()) if isinstance(raw, Mapping) else None
            specs = raw.get("specs", ()) if isinstance(raw, Mapping) else None
            if not isinstance(profile_id, str) or not profile_id or not isinstance(role, str) or not role or not isinstance(dut_instance, str) or not dut_instance or not isinstance(final_testbench, str) or not final_testbench or not isinstance(expected_corner_count, int) or expected_corner_count < 0 or not isinstance(stimuli, Mapping) or not isinstance(analyses, (list, tuple)) or not analyses or not isinstance(metrics, (list, tuple)) or not isinstance(specs, (list, tuple)):
                raise MaestroValidationError("Maestro profile context is incomplete")
            analysis_types = tuple(str(item.get("type")) for item in analyses if isinstance(item, Mapping))
            if len(analysis_types) != len(analyses):
                raise MaestroValidationError("Maestro profile analyses are invalid")
            maestro_testbench = result + "_" + profile_id + "_maestro_tb"
            profiles.append(MaestroProfileContext(profile_id, role, final_testbench, maestro_testbench, profile_id, dut_instance,
                                                   analysis_types, dict(stimuli), tuple(dict(item) for item in analyses), tuple(dict(item) for item in metrics), tuple(dict(item) for item in specs), expected_corner_count))
    else:
        final_testbench = result + "_tb"
        maestro_testbench = result + "_maestro_tb"
        profiles.append(MaestroProfileContext("default", "legacy", final_testbench, maestro_testbench, "amp_op_ac", published.dut_instance,
                                               ("ac",), dict(published.config.get("stimuli", {})), tuple(published.config.get("analyses", ())), tuple(published.config.get("metrics", ())), tuple(published.config.get("specs", ())), len(corners)))
    identifiers = {published.source_cell, published.work_cell, result, maestro_cell}
    identifiers.update(profile.final_testbench for profile in profiles)
    identifiers.update(profile.maestro_testbench for profile in profiles)
    if len(identifiers) != 4 + 2 * len(profiles):
        raise MaestroValidationError("Maestro delivery identifiers are not isolated")
    primary = profiles[0]
    return MaestroContext(published, primary.final_testbench, primary.maestro_testbench, maestro_cell, primary.test_name, corners,
                          _model_file(published.config), str(pvt.get("voltage_stimulus", "VDD")), tuple(profiles))


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


def compare_profile_metrics(direct: Mapping[str, Any], maestro: Mapping[str, Any], *, relative: float, absolute: float) -> dict[str, Any]:
    if not isinstance(direct, Mapping) or not isinstance(maestro, Mapping) or set(direct) != set(maestro) or not direct:
        raise MaestroValidationError("direct and Maestro profile metric sets must match")
    if isinstance(relative, bool) or isinstance(absolute, bool) or not all(isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) >= 0 for value in (relative, absolute)):
        raise MaestroValidationError("metric comparison tolerances must be finite and nonnegative")
    profiles = {}; overall = True
    for profile_id, direct_metrics in direct.items():
        maestro_metrics = maestro.get(profile_id)
        if not isinstance(direct_metrics, Mapping) or not isinstance(maestro_metrics, Mapping) or set(direct_metrics) != set(maestro_metrics) or not direct_metrics:
            raise MaestroValidationError("profile metric sets must match: " + str(profile_id))
        compared = {}; profile_passed = True
        for metric, expected in direct_metrics.items():
            actual = maestro_metrics[metric]
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in (expected, actual)):
                raise MaestroValidationError("profile metrics must be finite numbers")
            delta = abs(float(actual) - float(expected))
            limit = max(float(absolute), float(relative) * abs(float(expected)))
            passed = delta <= limit
            compared[str(metric)] = {"direct": float(expected), "maestro": float(actual), "delta": delta, "limit": limit, "passed": passed}
            profile_passed = profile_passed and passed
        profiles[str(profile_id)] = {"passed": profile_passed, "metrics": compared}
        overall = overall and profile_passed
    return {"passed": overall, "relative_tolerance": float(relative), "absolute_tolerance": float(absolute), "profiles": profiles}


def write_maestro_profile_confirmation(run_dir: str | Path, checks: Mapping[str, Any], details: Mapping[str, Any]) -> Path:
    required_profiles = details.get("required_profile_ids") if isinstance(details, Mapping) else None
    if not isinstance(required_profiles, (list, tuple)) or not required_profiles or len(set(required_profiles)) != len(required_profiles):
        raise MaestroValidationError("required Maestro profiles are invalid")
    expected_counts = details.get("expected_corner_counts", {})
    if not isinstance(expected_counts, Mapping):
        raise MaestroValidationError("expected Maestro corner counts must be a mapping")
    global_checks = details.get("global_checks")
    required_global = ("maestro_cell_exists", "maestro_testbenches_exist", "dut_uses_result_cell", "model_sections_verified", "profile_summary_hash_match")
    if not isinstance(global_checks, Mapping) or any(global_checks.get(name) is not True for name in required_global):
        raise MaestroValidationError("Maestro global structural checks are incomplete")
    required_true = ("test_exists", "run_completed", "history_exists", "reopen_check_passed", "metrics_match")
    normalized = {}
    for profile_id in required_profiles:
        profile_checks = checks.get(profile_id) if isinstance(checks, Mapping) else None
        if not isinstance(profile_checks, Mapping):
            raise MaestroValidationError("Maestro profile is missing: " + str(profile_id))
        if any(profile_checks.get(name) is not True for name in required_true):
            raise MaestroValidationError("Maestro profile checks are incomplete: " + str(profile_id))
        expected = expected_counts.get(profile_id, 45)
        if profile_checks.get("corner_count") != expected or profile_checks.get("failed_corner_count") != 0:
            raise MaestroValidationError("Maestro profile corner validation failed: " + str(profile_id))
        normalized[str(profile_id)] = dict(profile_checks)
    target = Path(run_dir) / "maestro_validation" / "maestro_validation.confirmed.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "status": "passed", "profiles": normalized, "details": dict(details)}
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

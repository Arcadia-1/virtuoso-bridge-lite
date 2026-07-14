"""Configuration schema for the version 2 analog optimization workflow."""

from __future__ import annotations

import json
import math
import re
from numbers import Real
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from analog_opt.profiles import VerificationProfileConfig
from analog_opt.units import UnitError, parse_quantity


class ConfigError(ValueError):
    """Raised when an analog optimization configuration is invalid."""


@dataclass(frozen=True)
class DesignConfig:
    library: str
    cell: str
    work_cell: str
    result_cell: str
    testbench_cell: str
    dut_instance: str = "DUT"


@dataclass(frozen=True)
class StimulusConfig:
    kind: str
    optimizable: bool = False
    value: Optional[float] = None
    dc: Optional[float] = None
    ac: Optional[float] = None
    lower: Optional[float] = None
    upper: Optional[float] = None
    source_instance: Optional[str] = None


@dataclass(frozen=True)
class AnalogOptConfig:
    version: int
    design: DesignConfig
    stimuli: Dict[str, StimulusConfig]
    parameters: List[Dict[str, Any]]
    analyses: List[Dict[str, Any]]
    metrics: List[Dict[str, Any]]
    specs: List[Dict[str, Any]]
    verification_profiles: Tuple[VerificationProfileConfig, ...]
    search: Dict[str, Any]
    pvt: Dict[str, Any]
    outputs: Dict[str, Any]


_REQUIRED_TOP_LEVEL = (
    "version",
    "design",
    "stimuli",
    "parameters",
    "analyses",
    "metrics",
    "specs",
    "search",
    "pvt",
    "outputs",
)
_REQUIRED_DESIGN = ("library", "cell", "work_cell", "result_cell", "testbench_cell")
_PARAMETER_TARGETS = {"virtuoso_cdf", "bias", "spectre_variable"}
_ANALYSIS_TYPES = {'dc_op', 'dc_sweep', 'ac', 'noise', 'tran', 'stb'}
_STIMULUS_DIMENSIONS = {"voltage": "voltage", "current": "current"}


_PROFILE_PVT_POLICIES = {'nominal_only', 'selected', 'full'}


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be a mapping")
    return value


def _list_of_mappings(value: Any, location: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise ConfigError(f"{location} must be a list")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"{location}[{index}] must be a mapping")
        result.append(dict(item))
    return result


def _require_keys(data: Mapping[str, Any], keys: Any, location: str) -> None:
    for key in keys:
        if key not in data:
            raise ConfigError(f"missing required key: {location}{key}")


def _nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location} must be a nonempty string")
    return value


def _parse_design(value: Any) -> DesignConfig:
    data = _mapping(value, "design")
    _require_keys(data, _REQUIRED_DESIGN, "design.")
    fields = {
        key: _nonempty_string(data[key], f"design.{key}")
        for key in _REQUIRED_DESIGN
    }
    fields["dut_instance"] = _nonempty_string(data.get("dut_instance", "DUT"), "design.dut_instance")
    cells = [fields["cell"], fields["work_cell"], fields["result_cell"]]
    if len(set(cells)) != len(cells):
        raise ConfigError("design source, work, and result cells must be distinct")
    return DesignConfig(**fields)


def _finite_float(value: Real, location: str) -> float:
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ConfigError(f"{location} must be a finite number") from exc
    if not math.isfinite(result):
        raise ConfigError(f"{location} must be a finite number")
    return result


def _parse_quantity_value(value: Any, kind: str, location: str) -> float:
    if isinstance(value, Real) and not isinstance(value, bool):
        return _finite_float(value, location)
    if isinstance(value, str):
        try:
            dimension = _STIMULUS_DIMENSIONS[kind]
        except KeyError as exc:
            raise ConfigError(
                f"{location} cannot parse quantity for stimulus kind: {kind}"
            ) from exc
        try:
            return parse_quantity(value, dimension)
        except UnitError as exc:
            raise ConfigError(f"{location} must be a valid quantity: {exc}") from exc
    raise ConfigError(f"{location} must be a number or quantity")


def _parse_ac(value: Any, location: str) -> float:
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ConfigError(f"{location} must be a finite number")
    return _finite_float(value, location)


def _parse_stimuli(value: Any) -> Dict[str, StimulusConfig]:
    data = _mapping(value, "stimuli")
    stimuli = {}
    for name, raw_stimulus in data.items():
        stimulus = _mapping(raw_stimulus, f"stimuli.{name}")
        _require_keys(stimulus, ("kind",), f"stimuli.{name}.")
        kind = stimulus["kind"]
        if not isinstance(kind, str) or kind not in _STIMULUS_DIMENSIONS:
            raise ConfigError(f"stimuli.{name}.kind is unsupported: {kind}")
        value_parsed = (
            _parse_quantity_value(stimulus["value"], kind, f"stimuli.{name}.value")
            if "value" in stimulus
            else None
        )
        dc_parsed = (
            _parse_quantity_value(stimulus["dc"], kind, f"stimuli.{name}.dc")
            if "dc" in stimulus
            else None
        )
        ac_parsed = (
            _parse_ac(stimulus["ac"], f"stimuli.{name}.ac")
            if "ac" in stimulus
            else None
        )
        optimizable = stimulus.get("optimizable", False)
        if not isinstance(optimizable, bool):
            raise ConfigError(f"stimuli.{name}.optimizable must be a boolean")
        if optimizable:
            _require_keys(stimulus, ("lower", "upper"), f"stimuli.{name}.")
            lower_value = _parse_quantity_value(
                stimulus["lower"], kind, f"stimuli.{name}.lower"
            )
            upper_value = _parse_quantity_value(
                stimulus["upper"], kind, f"stimuli.{name}.upper"
            )
            if lower_value >= upper_value:
                raise ConfigError(f"stimuli.{name} bounds require lower < upper")
        source_instance = _nonempty_string(stimulus.get("source_instance", "SRC_" + name), f"stimuli.{name}.source_instance")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.$]*", source_instance) is None:
            raise ConfigError(f"stimuli.{name}.source_instance must be a safe identifier")
        stimuli[name] = StimulusConfig(
            kind=kind,
            optimizable=optimizable,
            value=value_parsed,
            dc=dc_parsed,
            ac=ac_parsed,
            lower=lower_value if optimizable else None,
            upper=upper_value if optimizable else None,
            source_instance=source_instance,
        )
    if len({item.source_instance for item in stimuli.values()}) != len(stimuli):
        raise ConfigError("stimuli source_instance values must be unique")
    return stimuli


def _validate_named_items(
    items: List[Dict[str, Any]],
    location: str,
    discriminator: str,
    allowed: Any,
) -> None:
    names = set()
    singular = {'target': 'parameter', 'type': 'analysis'}[discriminator]
    for index, item in enumerate(items):
        _require_keys(item, ("name", discriminator), f"{location}[{index}].")
        name = _nonempty_string(item["name"], f"{singular} name")
        discriminator_value = item[discriminator]
        if not isinstance(discriminator_value, str):
            raise ConfigError(f"{singular} {discriminator} must be a string")
        if name in names:
            raise ConfigError(f"{singular} names must be unique: {name}")
        names.add(name)
        if discriminator_value not in allowed:
            raise ConfigError(
                f"unsupported {singular} {discriminator}: {discriminator_value}"
            )


def _stimuli_payload(stimuli: Mapping[str, StimulusConfig]) -> Dict[str, Dict[str, Any]]:
    return {
        name: {
            key: item
            for key, item in asdict(stimulus).items()
            if item is not None
        }
        for name, stimulus in stimuli.items()
    }


def _profile_metric_references(
    analyses: List[Dict[str, Any]], metrics: List[Dict[str, Any]]
) -> Tuple[set, Tuple[str, ...]]:
    exact = set()
    for metric in metrics:
        name = metric.get('name', metric.get('metric'))
        if isinstance(name, str) and name:
            if name in exact:
                raise ConfigError('verification profile metric name must be unique')
            exact.add(name)
    prefixes = tuple(
        '%s.%s.' % (analysis['type'], analysis['name'])
        for analysis in analyses
    )
    return exact, prefixes


def _parse_verification_profiles(
    value: Any,
    design: DesignConfig,
    stimuli: Mapping[str, StimulusConfig],
    analyses: List[Dict[str, Any]],
    metrics: List[Dict[str, Any]],
    specs: List[Dict[str, Any]],
) -> Tuple[VerificationProfileConfig, ...]:
    if value is None:
        return (VerificationProfileConfig(
            id='default', role='legacy', testbench_cell=design.testbench_cell,
            dut_instance=design.dut_instance, stimuli=_stimuli_payload(stimuli),
            analyses=tuple(dict(item) for item in analyses),
            metrics=tuple(dict(item) for item in metrics),
            specs=tuple(dict(item) for item in specs),
        ),)
    raw_profiles = _list_of_mappings(value, 'verification_profiles')
    if not raw_profiles:
        raise ConfigError('verification_profiles must be a nonempty list')
    parsed = []
    profile_ids = set()
    testbench_cells = set()
    for index, raw in enumerate(raw_profiles):
        location = 'verification_profiles[%d]' % index
        _require_keys(raw, ('id', 'role', 'testbench_cell', 'dut_instance',
                            'stimuli', 'analyses', 'metrics', 'specs'), location + '.')
        profile_id = _nonempty_string(raw['id'], location + '.id')
        if profile_id in profile_ids:
            raise ConfigError('verification profile id must be unique')
        profile_ids.add(profile_id)
        testbench_cell = _nonempty_string(raw['testbench_cell'], location + '.testbench_cell')
        if testbench_cell in testbench_cells:
            raise ConfigError('verification profile testbench cell must be unique')
        testbench_cells.add(testbench_cell)
        parsed_stimuli = _parse_stimuli(raw['stimuli'])
        parsed_analyses = _list_of_mappings(raw['analyses'], location + '.analyses')
        _validate_named_items(parsed_analyses, location + '.analyses', 'type', _ANALYSIS_TYPES)
        parsed_metrics = _list_of_mappings(raw['metrics'], location + '.metrics')
        parsed_specs = _list_of_mappings(raw['specs'], location + '.specs')
        exact_metrics, metric_prefixes = _profile_metric_references(parsed_analyses, parsed_metrics)
        for spec in parsed_specs:
            metric = _nonempty_string(spec.get('metric'), location + '.specs.metric')
            if metric not in exact_metrics and not metric.startswith(metric_prefixes):
                raise ConfigError('verification profile spec metric must exist')
        policy = raw.get('pvt_policy', 'full')
        if policy not in _PROFILE_PVT_POLICIES:
            raise ConfigError(location + '.pvt_policy is invalid')
        timeout = raw.get('timeout_s', 1800)
        if type(timeout) is not int or timeout <= 0:
            raise ConfigError(location + '.timeout_s must be a positive integer')
        parsed.append(VerificationProfileConfig(
            id=profile_id,
            role=_nonempty_string(raw['role'], location + '.role'),
            testbench_cell=testbench_cell,
            dut_instance=_nonempty_string(raw['dut_instance'], location + '.dut_instance'),
            stimuli=_stimuli_payload(parsed_stimuli),
            analyses=tuple(parsed_analyses), metrics=tuple(parsed_metrics),
            specs=tuple(parsed_specs), pvt_policy=policy, timeout_s=timeout,
        ))
    return tuple(parsed)


def _parse_pvt(value: Any, stimuli: Mapping[str, StimulusConfig]) -> Dict[str, Any]:
    raw = dict(_mapping(value, "pvt"))
    allowed = {"TT", "FF", "SS", "FNSP", "SNFP"}
    corners_raw = raw.get("corners", ["TT"])
    if not isinstance(corners_raw, list) or not corners_raw:
        raise ConfigError("pvt.corners must be a nonempty list")
    corners = []
    for index, corner in enumerate(corners_raw):
        if not isinstance(corner, str) or corner.upper() not in allowed:
            raise ConfigError(f"pvt.corners[{index}] is an unsupported corner")
        normalized = corner.upper()
        if normalized in corners:
            raise ConfigError("pvt.corners must be unique")
        corners.append(normalized)
    voltages_raw = raw.get("voltages", [])
    if not isinstance(voltages_raw, list):
        raise ConfigError("pvt.voltages must be a list")
    voltages = []
    for index, voltage in enumerate(voltages_raw):
        parsed = _parse_quantity_value(voltage, "voltage", f"pvt.voltages[{index}]")
        if parsed <= 0:
            raise ConfigError(f"pvt.voltages[{index}] must be positive")
        if parsed in voltages:
            raise ConfigError("pvt.voltages must be unique")
        voltages.append(parsed)
    temperatures_raw = raw.get("temperatures_c", raw.get("temperatures", [25.0]))
    if not isinstance(temperatures_raw, list) or not temperatures_raw:
        raise ConfigError("pvt.temperatures_c must be a nonempty list")
    temperatures = []
    for index, temperature in enumerate(temperatures_raw):
        if not isinstance(temperature, Real) or isinstance(temperature, bool):
            raise ConfigError(f"pvt.temperatures_c[{index}] must be a finite number")
        parsed = _finite_float(temperature, f"pvt.temperatures_c[{index}]")
        if parsed in temperatures:
            raise ConfigError("pvt.temperatures_c must be unique")
        temperatures.append(parsed)
    voltage_stimulus = raw.get("voltage_stimulus")
    if voltages and voltage_stimulus is None:
        raise ConfigError("pvt.voltage_stimulus is required when voltages are configured")
    if voltage_stimulus is not None and (voltage_stimulus not in stimuli or stimuli[voltage_stimulus].kind != "voltage"):
        raise ConfigError("pvt.voltage_stimulus must reference a voltage stimulus")
    result = {"corners": corners, "voltages": voltages, "temperatures_c": temperatures}
    if voltage_stimulus is not None:
        result["voltage_stimulus"] = voltage_stimulus
    return result


def _omit_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _omit_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_none(item) for item in value]
    return value


def canonical_resolved_payload(config: AnalogOptConfig) -> Dict[str, Any]:
    """Return a reloadable V2 payload with normalized SI quantities."""
    if not isinstance(config, AnalogOptConfig):
        raise ConfigError("resolved payload requires AnalogOptConfig")
    return _omit_none(asdict(config))


def load_config(path: Union[str, Path]) -> AnalogOptConfig:
    """Load and validate a version 2 analog optimization JSON config."""
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"failed to load config {config_path}: {exc}") from exc

    data = _mapping(raw, "config")
    _require_keys(data, _REQUIRED_TOP_LEVEL, "")
    if type(data["version"]) is not int or data["version"] != 2:
        raise ConfigError("version must be 2")

    parameters = _list_of_mappings(data["parameters"], "parameters")
    analyses = _list_of_mappings(data["analyses"], "analyses")
    _validate_named_items(parameters, "parameters", "target", _PARAMETER_TARGETS)
    _validate_named_items(analyses, "analyses", "type", _ANALYSIS_TYPES)

    parsed_stimuli = _parse_stimuli(data["stimuli"])
    for index, parameter in enumerate(parameters):
        if parameter.get("target") == "bias":
            stimulus_name = parameter.get("stimulus")
            if stimulus_name not in parsed_stimuli:
                raise ConfigError(f"parameters[{index}] bias stimulus must exist")
            if parsed_stimuli[stimulus_name].optimizable is not True:
                raise ConfigError(f"parameters[{index}] bias stimulus must be optimizable")
    design = _parse_design(data['design'])
    metrics = _list_of_mappings(data['metrics'], 'metrics')
    specs = _list_of_mappings(data['specs'], 'specs')
    verification_profiles = _parse_verification_profiles(
        data.get('verification_profiles'), design, parsed_stimuli,
        analyses, metrics, specs,
    )
    pvt = _parse_pvt(data['pvt'], parsed_stimuli)
    return AnalogOptConfig(
        version=2,
        design=design,
        stimuli=parsed_stimuli,
        parameters=parameters,
        analyses=analyses,
        metrics=metrics,
        specs=specs,
        verification_profiles=verification_profiles,
        search=dict(_mapping(data["search"], "search")),
        pvt=pvt,
        outputs=dict(_mapping(data["outputs"], "outputs")),
    )

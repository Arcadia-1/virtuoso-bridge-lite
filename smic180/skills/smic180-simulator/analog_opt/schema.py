"""Configuration schema for the version 2 analog optimization workflow."""

from __future__ import annotations

import json
import math
from numbers import Real
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Union

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


@dataclass(frozen=True)
class StimulusConfig:
    kind: str
    optimizable: bool = False
    value: Any = None
    dc: Any = None
    ac: Any = None
    lower: Any = None
    upper: Any = None


@dataclass(frozen=True)
class AnalogOptConfig:
    version: int
    design: DesignConfig
    stimuli: Dict[str, StimulusConfig]
    parameters: List[Dict[str, Any]]
    analyses: List[Dict[str, Any]]
    metrics: List[Dict[str, Any]]
    specs: List[Dict[str, Any]]
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
_ANALYSIS_TYPES = {"dc_op", "dc_sweep", "ac", "noise", "tran"}
_STIMULUS_DIMENSIONS = {"voltage": "voltage", "current": "current"}


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


def _parse_design(value: Any) -> DesignConfig:
    data = _mapping(value, "design")
    _require_keys(data, _REQUIRED_DESIGN, "design.")
    cells = [data["cell"], data["work_cell"], data["result_cell"]]
    if len(set(cells)) != len(cells):
        raise ConfigError("design source, work, and result cells must be distinct")
    return DesignConfig(
        library=data["library"],
        cell=data["cell"],
        work_cell=data["work_cell"],
        result_cell=data["result_cell"],
        testbench_cell=data["testbench_cell"],
    )


def _parse_bound(value: Any, kind: str, location: str) -> float:
    if isinstance(value, Real) and not isinstance(value, bool):
        result = float(value)
        if not math.isfinite(result):
            raise ConfigError(f"{location} must be finite")
        return result
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


def _parse_stimuli(value: Any) -> Dict[str, StimulusConfig]:
    data = _mapping(value, "stimuli")
    stimuli = {}
    for name, raw_stimulus in data.items():
        stimulus = _mapping(raw_stimulus, f"stimuli.{name}")
        _require_keys(stimulus, ("kind",), f"stimuli.{name}.")
        optimizable = stimulus.get("optimizable", False)
        if not isinstance(optimizable, bool):
            raise ConfigError(f"stimuli.{name}.optimizable must be a boolean")
        if optimizable:
            _require_keys(stimulus, ("lower", "upper"), f"stimuli.{name}.")
            lower_value = _parse_bound(
                stimulus["lower"], stimulus["kind"], f"stimuli.{name}.lower"
            )
            upper_value = _parse_bound(
                stimulus["upper"], stimulus["kind"], f"stimuli.{name}.upper"
            )
            if lower_value >= upper_value:
                raise ConfigError(f"stimuli.{name} bounds require lower < upper")
        stimuli[name] = StimulusConfig(
            kind=stimulus["kind"],
            optimizable=optimizable,
            value=stimulus.get("value"),
            dc=stimulus.get("dc"),
            ac=stimulus.get("ac"),
            lower=stimulus.get("lower"),
            upper=stimulus.get("upper"),
        )
    return stimuli


def _validate_named_items(
    items: List[Dict[str, Any]],
    location: str,
    discriminator: str,
    allowed: Any,
) -> None:
    names = set()
    singular = {"parameters": "parameter", "analyses": "analysis"}[location]
    for index, item in enumerate(items):
        _require_keys(item, ("name", discriminator), f"{location}[{index}].")
        name = item["name"]
        if name in names:
            raise ConfigError(f"{singular} names must be unique: {name}")
        names.add(name)
        if item[discriminator] not in allowed:
            raise ConfigError(
                f"unsupported {singular} {discriminator}: {item[discriminator]}"
            )


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

    return AnalogOptConfig(
        version=2,
        design=_parse_design(data["design"]),
        stimuli=_parse_stimuli(data["stimuli"]),
        parameters=parameters,
        analyses=analyses,
        metrics=_list_of_mappings(data["metrics"], "metrics"),
        specs=_list_of_mappings(data["specs"], "specs"),
        search=dict(_mapping(data["search"], "search")),
        pvt=dict(_mapping(data["pvt"], "pvt")),
        outputs=dict(_mapping(data["outputs"], "outputs")),
    )

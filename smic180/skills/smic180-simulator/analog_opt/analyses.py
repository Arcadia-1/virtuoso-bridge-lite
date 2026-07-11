"""Validation and Spectre line generation for analog analyses."""

from __future__ import annotations

import math
import re
from numbers import Real
from typing import Any, Dict, Iterable, List, Mapping

from analog_opt.units import UnitError, parse_quantity


class AnalysisError(ValueError):
    """Raised when an analysis declaration is invalid."""


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ANALYSIS_TYPES = {"dc_op", "dc_sweep", "ac", "noise", "tran"}
_ERRPRESETS = {"liberal", "moderate", "conservative"}


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AnalysisError(f"{location} must be a mapping")
    return value


def _name(value: Any, location: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise AnalysisError(f"{location} must be a valid Spectre name")
    return value


def _required_name(analysis: Mapping[str, Any], key: str) -> str:
    if key not in analysis:
        raise AnalysisError(f"analysis requires {key}")
    return _name(analysis[key], key)


def _number(value: Any, dimension: str, location: str) -> float:
    if isinstance(value, Real) and not isinstance(value, bool):
        try:
            result = float(value)
        except (OverflowError, ValueError) as exc:
            raise AnalysisError(f"{location} must be a finite number") from exc
        if not math.isfinite(result):
            raise AnalysisError(f"{location} must be a finite number")
        return result
    if isinstance(value, str):
        try:
            return parse_quantity(value, dimension)
        except UnitError as exc:
            raise AnalysisError(f"{location} must be a valid finite {dimension}") from exc
    raise AnalysisError(f"{location} must be a finite number or quantity")


def _required_number(analysis: Mapping[str, Any], key: str, dimension: str) -> float:
    if key not in analysis:
        raise AnalysisError(f"analysis requires {key}")
    return _number(analysis[key], dimension, key)


def _integer(analysis: Mapping[str, Any], key: str, minimum: int) -> int:
    if key not in analysis:
        raise AnalysisError(f"analysis requires {key}")
    value = analysis[key]
    if type(value) is not int:
        raise AnalysisError(f"{key} must be an integer")
    if value < minimum:
        if minimum == 2:
            raise AnalysisError(f"{key} must be at least 2")
        raise AnalysisError(f"{key} must be positive")
    return value


def _format(value: float) -> str:
    return f"{value:.12g}"


def _frequency_sweep(analysis: Mapping[str, Any]) -> tuple[float, float, int]:
    start = _required_number(analysis, "start", "frequency")
    stop = _required_number(analysis, "stop", "frequency")
    if start <= 0 or stop <= start:
        raise AnalysisError("frequency sweep requires 0 < start < stop")
    points = _integer(analysis, "points_per_decade", 1)
    return start, stop, points


def _validate_header(analysis: Mapping[str, Any]) -> tuple[str, str]:
    name = _required_name(analysis, "name")
    if "type" not in analysis or not isinstance(analysis["type"], str):
        raise AnalysisError("analysis requires type")
    analysis_type = analysis["type"]
    if analysis_type not in _ANALYSIS_TYPES:
        raise AnalysisError(f"unsupported analysis type: {analysis_type}")
    return name, analysis_type


def build_analysis_lines(analyses: Iterable[Mapping[str, Any]]) -> List[str]:
    """Validate declarations and return Spectre analysis statements."""
    lines: List[str] = []
    names = set()
    for index, raw_analysis in enumerate(analyses):
        analysis = _mapping(raw_analysis, f"analyses[{index}]")
        name, analysis_type = _validate_header(analysis)
        if name in names:
            raise AnalysisError(f"analysis names must be unique: {name}")
        names.add(name)

        if analysis_type == "dc_op":
            lines.append(f"{name} dc")
        elif analysis_type == "dc_sweep":
            parameter = _required_name(analysis, "parameter")
            _required_name(analysis, "source")
            start = _required_number(analysis, "start", "voltage")
            stop = _required_number(analysis, "stop", "voltage")
            if stop <= start:
                raise AnalysisError("dc sweep requires start < stop")
            points = _integer(analysis, "points", 2)
            lines.append(
                f"{name} dc param={parameter} start={_format(start)} "
                f"stop={_format(stop)} lin={points}"
            )
        elif analysis_type == "ac":
            start, stop, points = _frequency_sweep(analysis)
            lines.append(
                f"{name} ac start={_format(start)} stop={_format(stop)} dec={points}"
            )
        elif analysis_type == "noise":
            input_source = _required_name(analysis, "input_source")
            output = _required_name(analysis, "output")
            start, stop, points = _frequency_sweep(analysis)
            lines.append(
                f"{name} noise iprobe={input_source} oprobe={output} "
                f"start={_format(start)} stop={_format(stop)} dec={points}"
            )
        else:
            stop = _required_number(analysis, "stop", "time")
            if stop <= 0:
                raise AnalysisError("transient stop must be positive")
            parts = [f"{name} tran stop={_format(stop)}"]
            if "max_step" in analysis:
                max_step = _number(analysis["max_step"], "time", "max_step")
                if max_step <= 0:
                    raise AnalysisError("max_step must be positive")
                parts.append(f"maxstep={_format(max_step)}")
            if "errpreset" in analysis:
                errpreset = analysis["errpreset"]
                if errpreset not in _ERRPRESETS:
                    raise AnalysisError("errpreset must be liberal, moderate, or conservative")
                parts.append(f"errpreset={errpreset}")
            lines.append(" ".join(parts))
    return lines


def required_source_parameters(
    analyses: Iterable[Mapping[str, Any]],
) -> Dict[str, str]:
    """Return source-to-parameter patches required by DC sweeps."""
    result: Dict[str, str] = {}
    for index, raw_analysis in enumerate(analyses):
        analysis = _mapping(raw_analysis, f"analyses[{index}]")
        if analysis.get("type") != "dc_sweep":
            continue
        source = _required_name(analysis, "source")
        parameter = _required_name(analysis, "parameter")
        previous = result.get(source)
        if previous is not None and previous != parameter:
            raise AnalysisError(
                f"conflicting parameter mapping for source {source}: "
                f"{previous} versus {parameter}"
            )
        result[source] = parameter
    return result


def is_curve_analysis(analysis: Mapping[str, Any]) -> bool:
    """Return whether an analysis produces curve data suitable for plotting."""
    return isinstance(analysis, Mapping) and analysis.get("type") in {
        "dc_sweep",
        "ac",
        "noise",
        "tran",
    }

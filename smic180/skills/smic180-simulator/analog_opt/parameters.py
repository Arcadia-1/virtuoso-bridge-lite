"""Parameter transforms for analog optimization search spaces."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union


Number = Union[int, float]


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    target: str
    lower: Number
    upper: Number
    dtype: str = "float"
    scale: str = "linear"
    step: Optional[Number] = None
    instance: Optional[str] = None
    property: Optional[str] = None
    variable: Optional[str] = None
    stimulus: Optional[str] = None
    unit: Optional[str] = None


class ParameterSpace:
    """Convert between normalized optimizer vectors and named physical values."""

    def __init__(self, specs: Sequence[ParameterSpec]) -> None:
        self.specs = tuple(specs)
        self._validate_specs()

    @staticmethod
    def _finite_number(value: Any, location: str) -> float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("%s must be a finite number" % location)
        try:
            result = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("%s must be a finite number" % location) from exc
        if not math.isfinite(result):
            raise ValueError("%s must be a finite number" % location)
        return result

    @staticmethod
    def _require_finite(value: float, location: str) -> float:
        if not math.isfinite(value):
            raise ValueError("%s must be finite" % location)
        return value

    @classmethod
    def _stable_fraction(cls, value: float, lower: float, upper: float, location: str) -> float:
        scale = max(abs(lower), abs(upper), 1.0)
        scaled_lower = cls._require_finite(lower / scale, "%s lower ratio" % location)
        scaled_upper = cls._require_finite(upper / scale, "%s upper ratio" % location)
        scaled_value = cls._require_finite(value / scale, "%s value ratio" % location)
        denominator = cls._require_finite(
            scaled_upper - scaled_lower, "%s denominator" % location
        )
        numerator = cls._require_finite(
            scaled_value - scaled_lower, "%s numerator" % location
        )
        return cls._require_finite(numerator / denominator, location)

    def _validate_specs(self) -> None:
        names = []
        for spec in self.specs:
            if not isinstance(spec, ParameterSpec):
                raise ValueError("specs must contain ParameterSpec values")
            if not isinstance(spec.name, str) or not spec.name.strip():
                raise ValueError("parameter name must be a nonempty string")
            lower = self._finite_number(spec.lower, "%s lower bound" % spec.name)
            upper = self._finite_number(spec.upper, "%s upper bound" % spec.name)
            if lower >= upper:
                raise ValueError("parameter bounds require lower < upper")
            if spec.dtype not in ("float", "int"):
                raise ValueError("parameter dtype must be 'float' or 'int'")
            if spec.dtype == "int" and not (lower.is_integer() and upper.is_integer()):
                raise ValueError("int parameters require integer-valued bounds")
            if spec.scale not in ("linear", "log"):
                raise ValueError("parameter scale must be 'linear' or 'log'")
            if spec.scale == "log" and (lower <= 0.0 or upper <= 0.0):
                raise ValueError("log parameter bounds must be positive")
            if spec.step is not None:
                step = self._finite_number(spec.step, "%s step" % spec.name)
                if step <= 0.0:
                    raise ValueError("parameter step must be positive")
                scale = max(abs(lower), abs(upper), step, 1.0)
                scaled_span = self._require_finite(
                    upper / scale - lower / scale, "%s scaled span" % spec.name
                )
                scaled_step = self._require_finite(
                    step / scale, "%s scaled step" % spec.name
                )
                if scaled_step == 0.0:
                    raise ValueError(
                        "%s finite representable step count must be finite" % spec.name
                    )
                step_count = self._require_finite(
                    scaled_span / scaled_step,
                    "%s finite representable step count" % spec.name,
                )
                if step_count < 0.0:
                    raise ValueError("%s finite representable step count is invalid" % spec.name)
            names.append(spec.name)
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return min(max(value, lower), upper)

    @classmethod
    def _linear_value(cls, normalized: float, lower: float, upper: float, location: str) -> float:
        if normalized == 0.0:
            return lower
        if normalized == 1.0:
            return upper
        left = cls._require_finite((1.0 - normalized) * lower, "%s lower term" % location)
        right = cls._require_finite(normalized * upper, "%s upper term" % location)
        return cls._require_finite(left + right, location)

    def materialize(self, normalized_values: Sequence[Number]) -> Dict[str, Number]:
        if len(normalized_values) != len(self.specs):
            raise ValueError("normalized vector length must match parameter space")
        result = {}
        for spec, raw_normalized in zip(self.specs, normalized_values):
            normalized = self._finite_number(raw_normalized, "%s normalized value" % spec.name)
            normalized = self._clamp(normalized, 0.0, 1.0)
            lower = float(spec.lower)
            upper = float(spec.upper)
            if spec.scale == "log":
                log_value = self._linear_value(
                    normalized, math.log(lower), math.log(upper), "%s log interpolation" % spec.name
                )
                value = self._require_finite(math.exp(log_value), "%s materialized value" % spec.name)
            else:
                value = self._linear_value(normalized, lower, upper, "%s materialized value" % spec.name)
            if spec.step is not None:
                step = float(spec.step)
                scale = max(abs(lower), abs(upper), step, 1.0)
                scaled_value = self._require_finite(
                    value / scale, "%s scaled quantization value" % spec.name
                )
                scaled_lower = self._require_finite(
                    lower / scale, "%s scaled quantization lower" % spec.name
                )
                scaled_step = self._require_finite(
                    step / scale, "%s scaled quantization step" % spec.name
                )
                offset = self._require_finite(
                    scaled_value - scaled_lower, "%s quantization offset" % spec.name
                )
                steps = self._require_finite(
                    offset / scaled_step, "%s quantization step count" % spec.name
                )
                try:
                    rounded_steps = round(steps)
                except OverflowError as exc:
                    raise ValueError("%s quantization step count must be finite" % spec.name) from exc
                scaled_quantized = self._require_finite(
                    scaled_lower + rounded_steps * scaled_step,
                    "%s scaled quantized value" % spec.name,
                )
                value = self._require_finite(
                    scaled_quantized * scale, "%s quantized value" % spec.name
                )
            if spec.dtype == "int":
                value = int(round(value))
            value = self._require_finite(
                self._clamp(value, lower, upper), "%s final value" % spec.name
            )
            result[spec.name] = int(value) if spec.dtype == "int" else float(value)
        return result

    def normalize(self, values: Mapping[str, Number]) -> List[float]:
        expected = {spec.name for spec in self.specs}
        if set(values) != expected:
            raise ValueError("parameter names must exactly match parameter space")
        result = []
        for spec in self.specs:
            value = self._finite_number(values[spec.name], "%s value" % spec.name)
            lower = float(spec.lower)
            upper = float(spec.upper)
            value = self._clamp(value, lower, upper)
            if spec.scale == "log":
                normalized = self._stable_fraction(
                    math.log(value), math.log(lower), math.log(upper), "%s normalized value" % spec.name
                )
            else:
                normalized = self._stable_fraction(
                    value, lower, upper, "%s normalized value" % spec.name
                )
            result.append(
                self._require_finite(
                    self._clamp(normalized, 0.0, 1.0), "%s normalized value" % spec.name
                )
            )
        return result

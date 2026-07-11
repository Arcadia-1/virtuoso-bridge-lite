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
            if spec.scale not in ("linear", "log"):
                raise ValueError("parameter scale must be 'linear' or 'log'")
            if spec.scale == "log" and (lower <= 0.0 or upper <= 0.0):
                raise ValueError("log parameter bounds must be positive")
            if spec.step is not None:
                step = self._finite_number(spec.step, "%s step" % spec.name)
                if step <= 0.0:
                    raise ValueError("parameter step must be positive")
            names.append(spec.name)
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return min(max(value, lower), upper)

    @staticmethod
    def _implicit_integer_grid(spec: ParameterSpec) -> bool:
        return (
            spec.dtype == "float"
            and spec.step is None
            and isinstance(spec.lower, int)
            and not isinstance(spec.lower, bool)
            and isinstance(spec.upper, int)
            and not isinstance(spec.upper, bool)
        )

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
                value = math.exp(math.log(lower) + normalized * (math.log(upper) - math.log(lower)))
            else:
                value = lower + normalized * (upper - lower)
            if spec.step is not None:
                value = lower + round((value - lower) / float(spec.step)) * float(spec.step)
            elif self._implicit_integer_grid(spec):
                value = float(round(value))
            if spec.dtype == "int":
                value = int(round(value))
            value = self._clamp(value, lower, upper)
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
                normalized = (math.log(value) - math.log(lower)) / (math.log(upper) - math.log(lower))
            else:
                normalized = (value - lower) / (upper - lower)
            result.append(self._clamp(normalized, 0.0, 1.0))
        return result

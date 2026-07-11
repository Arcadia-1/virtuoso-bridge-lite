"""Declarative specification evaluation for analog optimization metrics."""

from dataclasses import dataclass
import math
import sys
from typing import Mapping, Optional, Sequence, Tuple


_EPSILON = sys.float_info.epsilon
_MAX_FLOAT = sys.float_info.max
_SUPPORTED_OPS = frozenset((">=", "<=", "between"))


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("%s must be a finite number" % name)
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("%s must be a finite number" % name)
    return result


def _nonnegative_number(value: object, name: str) -> float:
    result = _finite_number(value, name)
    if result < 0.0:
        raise ValueError("%s must be nonnegative" % name)
    return result


def _saturated_add(left: float, right: float) -> float:
    if right > _MAX_FLOAT - left:
        return _MAX_FLOAT
    return left + right


def _saturated_multiply(left: float, right: float) -> float:
    if left == 0.0 or right == 0.0:
        return 0.0
    if left > _MAX_FLOAT / right:
        return _MAX_FLOAT
    return left * right


def _fractional_violation(delta: float, reference: float) -> float:
    denominator = max(abs(reference), _EPSILON)
    if delta > _MAX_FLOAT * denominator:
        return _MAX_FLOAT
    return delta / denominator


@dataclass(frozen=True)
class Spec:
    metric: str
    op: str
    value: Optional[float] = None
    lower: Optional[float] = None
    upper: Optional[float] = None
    weight: float = 1
    hard: bool = False
    tolerance: float = 0

    def __post_init__(self) -> None:
        if not isinstance(self.metric, str) or not self.metric.strip():
            raise ValueError("metric must be a nonempty string")
        if not isinstance(self.op, str) or self.op not in _SUPPORTED_OPS:
            raise ValueError("op must be one of >=, <=, or between")
        if not isinstance(self.hard, bool):
            raise TypeError("hard must be a bool")

        weight = _nonnegative_number(self.weight, "weight")
        tolerance = _nonnegative_number(self.tolerance, "tolerance")
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "tolerance", tolerance)

        if self.op == "between":
            lower = _finite_number(self.lower, "lower")
            upper = _finite_number(self.upper, "upper")
            if lower >= upper:
                raise ValueError("bounds must satisfy lower < upper")
            object.__setattr__(self, "lower", lower)
            object.__setattr__(self, "upper", upper)
        else:
            value = _finite_number(self.value, "value")
            object.__setattr__(self, "value", value)


@dataclass(frozen=True)
class SpecResult:
    spec: Spec
    actual: Optional[float]
    violation: float
    penalty: float
    passed: bool
    missing: bool

    @property
    def total(self) -> float:
        return _saturated_add(
            _saturated_multiply(self.violation, self.spec.weight), self.penalty
        )


@dataclass(frozen=True)
class SpecSummary:
    results: Tuple[SpecResult, ...]
    total: float
    passed: bool


def _evaluate_present(spec: Spec, actual: float) -> float:
    tolerance = spec.tolerance
    if spec.op == ">=":
        boundary = float(spec.value) - tolerance
        if actual >= boundary:
            return 0.0
        return _fractional_violation(boundary - actual, float(spec.value))
    if spec.op == "<=":
        boundary = float(spec.value) + tolerance
        if actual <= boundary:
            return 0.0
        return _fractional_violation(actual - boundary, float(spec.value))

    lower = float(spec.lower) - tolerance
    upper = float(spec.upper) + tolerance
    if actual < lower:
        return _fractional_violation(lower - actual, float(spec.lower))
    if actual > upper:
        return _fractional_violation(actual - upper, float(spec.upper))
    return 0.0


def evaluate_specs(
    metrics: Mapping[str, float],
    specs: Sequence[Spec],
    missing_penalty: float = 1e6,
    hard_penalty: float = 1e4,
) -> SpecSummary:
    """Evaluate specifications and return a finite aggregate violation score."""
    missing_cost = _nonnegative_number(missing_penalty, "missing penalty")
    hard_cost = _nonnegative_number(hard_penalty, "hard penalty")
    results = []
    total = 0.0
    all_passed = True

    for spec in specs:
        if not isinstance(spec, Spec):
            raise TypeError("specs must contain Spec instances")
        if spec.metric not in metrics:
            penalty = missing_cost
            if spec.hard:
                penalty = _saturated_add(penalty, hard_cost)
            result = SpecResult(spec, None, 0.0, penalty, False, True)
        else:
            actual = _finite_number(metrics[spec.metric], "metric %s" % spec.metric)
            violation = _evaluate_present(spec, actual)
            passed = violation == 0.0
            penalty = hard_cost if spec.hard and not passed else 0.0
            result = SpecResult(spec, actual, violation, penalty, passed, False)
        results.append(result)
        total = _saturated_add(total, result.total)
        all_passed = all_passed and result.passed

    return SpecSummary(tuple(results), total, all_passed)

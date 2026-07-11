import ast
import math
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from analog_opt.specs import Spec, SpecResult, SpecSummary, evaluate_specs


def test_greater_equal_fractional_violation():
    result = evaluate_specs({"gain": 48}, [Spec("gain", ">=", value=60, weight=2)])
    assert result.total == pytest.approx(0.4)
    assert result.results[0].violation == pytest.approx(0.2)
    assert not result.passed


def test_less_equal_passes():
    result = evaluate_specs({"power": 0.8e-3}, [Spec("power", "<=", value=1e-3)])
    assert result.total == 0
    assert result.passed


@pytest.mark.parametrize(("actual", "expected"), [(2.0, 0.5), (6.0, 0.0), (12.0, 0.2)])
def test_between_uses_fractional_violation_from_nearest_bound(actual, expected):
    result = evaluate_specs(
        {"bandwidth": actual},
        [Spec("bandwidth", "between", lower=4, upper=10)],
    )
    assert result.total == pytest.approx(expected)


def test_tolerance_expands_the_passing_region():
    result = evaluate_specs({"gain": 59}, [Spec("gain", ">=", value=60, tolerance=1)])
    assert result.total == 0
    assert result.passed


def test_zero_reference_uses_epsilon_and_remains_finite():
    result = evaluate_specs({"offset": -1}, [Spec("offset", ">=", value=0)])
    assert math.isfinite(result.total)
    assert result.total > 0


def test_hard_failure_adds_finite_penalty():
    result = evaluate_specs(
        {"gain": 48}, [Spec("gain", ">=", value=60, hard=True)], hard_penalty=1234
    )
    assert result.total == pytest.approx(1234.2)
    assert result.results[0].penalty == 1234


def test_missing_hard_metric_is_finite():
    result = evaluate_specs(
        {},
        [Spec("op.M1.margin", ">=", value=0.1, hard=True)],
        missing_penalty=1e5,
    )
    assert math.isfinite(result.total)
    assert result.total >= 1e5
    assert result.results[0].missing


def test_results_are_frozen_dataclasses():
    spec = Spec("gain", ">=", value=60)
    item = SpecResult(spec, 60.0, 0.0, 0.0, True, False)
    summary = SpecSummary((item,), 0.0, True)
    with pytest.raises(FrozenInstanceError):
        spec.weight = 2
    with pytest.raises(FrozenInstanceError):
        item.passed = False
    with pytest.raises(FrozenInstanceError):
        summary.total = 1


@pytest.mark.parametrize("op", [">", "=", "gte", "", None, True])
def test_rejects_invalid_operator(op):
    with pytest.raises((TypeError, ValueError), match="op"):
        Spec("gain", op, value=60)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"value": None}, "value"),
        ({"value": float("nan")}, "value"),
        ({"value": float("inf")}, "value"),
        ({"value": True}, "value"),
        ({"value": 1, "weight": -1}, "weight"),
        ({"value": 1, "weight": True}, "weight"),
        ({"value": 1, "tolerance": -1}, "tolerance"),
        ({"value": 1, "hard": 1}, "hard"),
    ],
)
def test_rejects_invalid_threshold_spec_fields(kwargs, match):
    with pytest.raises((TypeError, ValueError), match=match):
        Spec("gain", ">=", **kwargs)


@pytest.mark.parametrize(
    ("lower", "upper"),
    [(None, 1), (0, None), (1, 1), (2, 1), (False, 1), (0, float("inf"))],
)
def test_between_requires_finite_increasing_non_boolean_bounds(lower, upper):
    with pytest.raises((TypeError, ValueError), match="lower|upper|bounds"):
        Spec("gain", "between", lower=lower, upper=upper)


@pytest.mark.parametrize(
    ("metrics", "missing_penalty", "hard_penalty"),
    [
        ({"gain": True}, 1e6, 1e4),
        ({"gain": float("nan")}, 1e6, 1e4),
        ({"gain": float("inf")}, 1e6, 1e4),
        ({"gain": 1}, -1, 1e4),
        ({"gain": 1}, 1e6, float("inf")),
        ({"gain": 1}, True, 1e4),
    ],
)
def test_rejects_invalid_metrics_and_penalties(metrics, missing_penalty, hard_penalty):
    with pytest.raises((TypeError, ValueError), match="metric|penalty"):
        evaluate_specs(
            metrics,
            [Spec("gain", ">=", value=1)],
            missing_penalty=missing_penalty,
            hard_penalty=hard_penalty,
        )


def test_specs_module_uses_python_39_compatible_annotations():
    specs_path = Path(__file__).resolve().parents[2] / "skills" / "smic180-simulator" / "analog_opt" / "specs.py"
    module = ast.parse(specs_path.read_text(encoding="utf-8-sig"))
    annotations = [
        node.annotation
        for node in ast.walk(module)
        if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation is not None
    ]
    assert not any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
        for annotation in annotations
        for node in ast.walk(annotation)
    )


@pytest.mark.parametrize(
    ("actual", "spec"),
    [
        (-sys.float_info.max, Spec("metric", ">=", value=sys.float_info.max)),
        (sys.float_info.max, Spec("metric", "<=", value=-sys.float_info.max)),
        (
            sys.float_info.max,
            Spec(
                "metric",
                "between",
                lower=-sys.float_info.max,
                upper=-sys.float_info.max / 2,
            ),
        ),
    ],
)
def test_extreme_finite_inputs_produce_finite_violation_and_total(actual, spec):
    result = evaluate_specs({"metric": actual}, [spec])
    assert math.isfinite(result.results[0].violation)
    assert math.isfinite(result.total)

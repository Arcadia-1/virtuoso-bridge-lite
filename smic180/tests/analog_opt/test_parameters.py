import ast
import math
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from analog_opt.parameters import ParameterSpace, ParameterSpec


def test_parameter_spec_is_immutable_and_preserves_optional_metadata():
    spec = ParameterSpec("width", "virtuoso_cdf", 1.0, 40.0, instance="M1", property="w", variable="W", stimulus="VIN", unit="um")
    assert (spec.instance, spec.property, spec.variable, spec.stimulus, spec.unit) == ("M1", "w", "W", "VIN", "um")
    with pytest.raises(FrozenInstanceError):
        spec.lower = 2.0


def test_linear_materialize_and_normalize_clamp_to_bounds():
    space = ParameterSpace([ParameterSpec("x", "bias", 10.0, 20.0)])
    assert space.materialize([-1.0]) == {"x": 10.0}
    assert space.materialize([0.25]) == {"x": 12.5}
    assert space.materialize([2.0]) == {"x": 20.0}
    assert space.normalize({"x": 5.0}) == [0.0]
    assert space.normalize({"x": 12.5}) == [0.25]
    assert space.normalize({"x": 25.0}) == [1.0]


def test_log_transform_operates_in_log_domain():
    space = ParameterSpace([ParameterSpec("frequency", "spectre_variable", 1.0, 100.0, scale="log")])
    assert space.materialize([0.5])["frequency"] == pytest.approx(10.0)
    assert space.normalize({"frequency": 10.0}) == pytest.approx([0.5])


def test_step_is_quantized_after_denormalization_then_finally_clamped():
    space = ParameterSpace([ParameterSpec("x", "bias", 0.0, 10.0, step=6.0)])
    assert space.materialize([0.4]) == {"x": 6.0}
    assert space.materialize([1.0]) == {"x": 10.0}


def test_int_rounding_happens_after_denormalization():
    space = ParameterSpace([ParameterSpec("fingers", "virtuoso_cdf", 1, 8, dtype="int")])
    assert space.materialize([0.5]) == {"fingers": 4}
    assert isinstance(space.materialize([0.5])["fingers"], int)


def test_historical_exactly_once_transform_regression():
    space = ParameterSpace([ParameterSpec("x", "bias", 1, 40)])
    materialized = space.materialize([0.5])
    assert materialized == {"x": 20.0}
    normalized = space.normalize(materialized)
    assert normalized == pytest.approx([19.0 / 39.0])
    assert space.materialize(normalized) == {"x": 20.0}


@pytest.mark.parametrize("spec,match", [
    (ParameterSpec("", "bias", 1.0, 2.0), "name"),
    (ParameterSpec("x", "bias", 2.0, 1.0), "bounds"),
    (ParameterSpec("x", "bias", 1.0, 1.0), "bounds"),
    (ParameterSpec("x", "bias", 1.0, 2.0, dtype="decimal"), "dtype"),
    (ParameterSpec("x", "bias", 1.0, 2.0, scale="sqrt"), "scale"),
    (ParameterSpec("x", "bias", 0.0, 2.0, scale="log"), "positive"),
    (ParameterSpec("x", "bias", 1.0, 2.0, step=0.0), "step"),
])
def test_invalid_specs_are_rejected(spec, match):
    with pytest.raises(ValueError, match=match):
        ParameterSpace([spec])


def test_duplicate_parameter_names_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        ParameterSpace([ParameterSpec("x", "bias", 1.0, 2.0), ParameterSpec("x", "bias", 2.0, 3.0)])


@pytest.mark.parametrize("bad", [True, False, math.nan, math.inf, -math.inf])
def test_materialize_rejects_bool_and_nonfinite_values(bad):
    space = ParameterSpace([ParameterSpec("x", "bias", 1.0, 2.0)])
    with pytest.raises(ValueError, match="finite number"):
        space.materialize([bad])


@pytest.mark.parametrize("bad", [True, False, math.nan, math.inf, -math.inf])
def test_normalize_rejects_bool_and_nonfinite_values(bad):
    space = ParameterSpace([ParameterSpec("x", "bias", 1.0, 2.0)])
    with pytest.raises(ValueError, match="finite number"):
        space.normalize({"x": bad})


def test_materialize_validates_vector_length():
    space = ParameterSpace([ParameterSpec("x", "bias", 1.0, 2.0)])
    with pytest.raises(ValueError, match="length"):
        space.materialize([])
    with pytest.raises(ValueError, match="length"):
        space.materialize([0.5, 0.5])


@pytest.mark.parametrize("values", [{}, {"y": 1.0}, {"x": 1.0, "y": 2.0}])
def test_normalize_requires_exact_parameter_names(values):
    space = ParameterSpace([ParameterSpec("x", "bias", 1.0, 2.0)])
    with pytest.raises(ValueError, match="names"):
        space.normalize(values)


def test_parameter_bounds_and_step_reject_bool_and_nonfinite():
    for kwargs in ({"lower": True, "upper": 2.0}, {"lower": 1.0, "upper": math.inf}, {"lower": 1.0, "upper": 2.0, "step": math.nan}):
        with pytest.raises(ValueError, match="finite number"):
            ParameterSpace([ParameterSpec("x", "bias", **kwargs)])


def test_parameters_module_uses_python_39_compatible_annotations():
    path = Path(__file__).resolve().parents[2] / "skills" / "smic180-simulator" / "analog_opt" / "parameters.py"
    module = ast.parse(path.read_text(encoding="utf-8-sig"))
    annotations = [node.annotation for node in ast.walk(module) if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation is not None]
    assert not any(isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) for annotation in annotations for node in ast.walk(annotation))

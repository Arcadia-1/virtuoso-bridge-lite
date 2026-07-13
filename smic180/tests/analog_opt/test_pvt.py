import math

import pytest

from analog_opt.evaluator import EvaluationResult
from analog_opt.pvt import PvtConfig, build_pvt_points, pvt_result_from_evaluation, summarize_pvt


def result(point, success=True, objective=1.0, violation=0.0, failure=None):
    return {"point_id": point.point_id, "corner": point.corner, "voltage": point.voltage,
            "temperature": point.temperature, "parameters": {"w": 1e-6}, "metrics": {"gain": 60.0},
            "success": success, "objective": objective,
            "specs": {"gain": {"passed": violation == 0.0, "violation": violation}}, "failure": failure}


def test_build_points_is_corner_major_exact_and_path_safe():
    config = PvtConfig(("TT", "SS"), (1.8, math.nextafter(1.8, math.inf)), (-40.0, 125.0))
    points = build_pvt_points(config)
    assert [(p.corner, p.voltage, p.temperature) for p in points[:4]] == [
        ("tt", config.voltages[0], -40.0), ("tt", config.voltages[0], 125.0),
        ("tt", config.voltages[1], -40.0), ("tt", config.voltages[1], 125.0)]
    assert len({p.point_id for p in points}) == 8
    assert all("/" not in p.point_id and "\\" not in p.point_id and ".." not in p.point_id for p in points)
    assert points[0].point_id != points[2].point_id


@pytest.mark.parametrize("corners", [(), ("tt", "tt"), ("sf",)])
def test_config_rejects_bad_corners(corners):
    with pytest.raises(ValueError): PvtConfig(corners, (1.8,), (25.0,))


@pytest.mark.parametrize("voltages", [(), (0.0,), (-1.0,), (math.inf,), (1.8, 1.8)])
def test_config_rejects_bad_voltages(voltages):
    with pytest.raises(ValueError): PvtConfig(("tt",), voltages, (25.0,))


@pytest.mark.parametrize("temperatures", [(), (math.nan,), (25.0, 25.0)])
def test_config_rejects_bad_temperatures(temperatures):
    with pytest.raises(ValueError): PvtConfig(("tt",), (1.8,), temperatures)


def test_summary_failure_has_priority_and_preserves_context():
    points = build_pvt_points(PvtConfig(("tt",), (1.8,), (25.0, 125.0)))
    results = [result(points[0], objective=999.0, violation=0.8),
               result(points[1], success=False, objective=0.1, failure={"category": "timeout", "message": "late"})]
    summary = summarize_pvt(points, results, expected_spec_ids=("gain",))
    assert not summary.overall_passed
    assert summary.worst.point_id == points[1].point_id
    assert summary.worst.failure == {"category": "timeout", "message": "late"}
    assert summary.points[1]["parameters"] == {"w": 1e-6}
    assert summary.points[1]["metrics"] == {"gain": 60.0}


def test_summary_requires_complete_identical_specs_and_consistent_pass_semantics():
    points = build_pvt_points(PvtConfig(("tt",), (1.8,), (25.0, 125.0)))
    valid = [result(p) for p in points]
    assert summarize_pvt(points, valid).overall_passed
    for mutation in (
        lambda rows: rows[1].update(specs={}),
        lambda rows: rows[1]["specs"]["gain"].update(passed=False, violation=0.0),
        lambda rows: rows[1]["specs"]["gain"].update(passed=True, violation=0.1),
        lambda rows: rows[1]["specs"]["gain"].update(violation=-0.1),
        lambda rows: rows[1].update(corner="ss"),
        lambda rows: rows[1].update(parameters=[]),
        lambda rows: rows[1].update(metrics=[]),
    ):
        rows = [dict(r, specs={k: dict(v) for k, v in r["specs"].items()}) for r in valid]
        mutation(rows)
        with pytest.raises(ValueError): summarize_pvt(points, rows, expected_spec_ids=("gain",))
    with pytest.raises(ValueError): summarize_pvt(points, [dict(r, specs={}) for r in valid])
    with pytest.raises(ValueError): summarize_pvt(points, valid, expected_spec_ids=())


def test_evaluation_result_adapter_supports_success_and_failure():
    point = build_pvt_points(PvtConfig(("ss",), (1.62,), (125.0,)))[0]
    evaluation = EvaluationResult("candidate-1", 4.0, False, {"gain": 42.0}, {},
                                  {"category": "convergence", "message": "no convergence"},
                                  {"gain": {"passed": False, "violation": 0.3}})
    adapted = pvt_result_from_evaluation(point, evaluation, {"w": 2e-6})
    assert adapted["point_id"] == point.point_id and adapted["corner"] == "ss"
    assert adapted["parameters"] == {"w": 2e-6} and adapted["metrics"] == {"gain": 42.0}
    assert adapted["failure"]["category"] == "convergence"


@pytest.mark.parametrize("evaluation", [
    EvaluationResult("c", math.nan, True, {}, {}, None, {"gain": {"passed": True, "violation": 0.0}}),
    EvaluationResult("c", 1.0, 1, {}, {}, None, {"gain": {"passed": True, "violation": 0.0}}),
    EvaluationResult("c", 1.0, True, [], {}, None, {"gain": {"passed": True, "violation": 0.0}}),
    EvaluationResult("c", 1.0, True, {}, [], None, {"gain": {"passed": True, "violation": 0.0}}),
    EvaluationResult("c", 1.0, True, {}, {}, None, []),
    EvaluationResult("c", 1.0, True, {}, {}, {"category": "x", "message": "y"}, {"gain": {"passed": True, "violation": 0.0}}),
    EvaluationResult("c", 1.0, False, {}, {}, None, {"gain": {"passed": False, "violation": 0.1}}),
    EvaluationResult("c", 1.0, False, {}, {}, {"category": "", "message": "bad"}, {"gain": {"passed": False, "violation": 0.1}}),
])
def test_evaluation_adapter_rejects_invalid_protocol(evaluation):
    point = build_pvt_points(PvtConfig(("tt",), (1.8,), (25.0,)))[0]
    with pytest.raises(ValueError):
        pvt_result_from_evaluation(point, evaluation, {"w": 1e-6})


def test_evaluation_adapter_rejects_nonmapping_parameters():
    point = build_pvt_points(PvtConfig(("tt",), (1.8,), (25.0,)))[0]
    evaluation = EvaluationResult("c", 1.0, True, {}, {}, None,
                                  {"gain": {"passed": True, "violation": 0.0}})
    with pytest.raises(ValueError):
        pvt_result_from_evaluation(point, evaluation, [])

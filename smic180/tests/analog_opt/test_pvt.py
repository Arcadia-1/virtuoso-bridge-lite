import math

import pytest

from analog_opt.pvt import PvtConfig, build_pvt_points, summarize_pvt


def test_build_points_is_corner_major_and_stable():
    points = build_pvt_points(PvtConfig(("TT", "SS"), (1.8, 1.62), (-40.0, 125.0)))
    assert [(p.corner, p.voltage, p.temperature) for p in points] == [
        ("tt", 1.8, -40.0), ("tt", 1.8, 125.0), ("tt", 1.62, -40.0), ("tt", 1.62, 125.0),
        ("ss", 1.8, -40.0), ("ss", 1.8, 125.0), ("ss", 1.62, -40.0), ("ss", 1.62, 125.0),
    ]
    assert len({p.point_id for p in points}) == 8
    assert all("/" not in p.point_id and "\\" not in p.point_id and ".." not in p.point_id for p in points)


@pytest.mark.parametrize("corners", [(), ("tt", "tt"), ("sf",)])
def test_config_rejects_bad_corners(corners):
    with pytest.raises(ValueError):
        PvtConfig(corners, (1.8,), (25.0,))


@pytest.mark.parametrize("voltages", [(), (0.0,), (-1.0,), (math.inf,), (1.8, 1.8)])
def test_config_rejects_bad_voltages(voltages):
    with pytest.raises(ValueError):
        PvtConfig(("tt",), voltages, (25.0,))


@pytest.mark.parametrize("temperatures", [(), (math.nan,), (25.0, 25.0)])
def test_config_rejects_bad_temperatures(temperatures):
    with pytest.raises(ValueError):
        PvtConfig(("tt",), (1.8,), temperatures)


def test_summary_selects_severity_and_failure_blocks_overall():
    points = build_pvt_points(PvtConfig(("tt", "ss"), (1.8, 1.62), (25.0, 125.0)))
    results = [{"point_id": p.point_id, "success": True, "objective": 1.0,
                "specs": {"gain": {"passed": True, "violation": 0.0}}} for p in points]
    results[3]["specs"]["gain"] = {"passed": False, "violation": 0.2}
    results[-1] = {"point_id": points[-1].point_id, "success": False, "objective": 99.0,
                   "specs": {}, "failure": {"category": "convergence", "message": "did not converge"}}
    summary = summarize_pvt(points, results)
    assert not summary.overall_passed
    assert summary.worst.point_id == points[-1].point_id
    assert summary.worst.failure == {"category": "convergence", "message": "did not converge"}
    assert summary.worst_by_spec["gain"].point_id == points[3].point_id


def test_summary_ties_follow_input_order_and_requires_complete_unique_results():
    points = build_pvt_points(PvtConfig(("tt",), (1.8,), (25.0, 125.0)))
    tied = [{"point_id": p.point_id, "success": True, "objective": 2.0,
             "specs": {"gain": {"passed": False, "violation": 0.1}}} for p in points]
    assert summarize_pvt(points, tied).worst.point_id == points[0].point_id
    with pytest.raises(ValueError):
        summarize_pvt(points, tied[:1])
    with pytest.raises(ValueError):
        summarize_pvt(points, [tied[0], tied[0]])
    with pytest.raises(ValueError):
        summarize_pvt(points, [dict(tied[0], success=1), tied[1]])

import json
import os
from pathlib import Path

import pytest

from analog_opt.pvt import PvtConfig, build_pvt_points, summarize_pvt
from analog_opt.report import ReportError, write_pvt_results, write_report, write_result_manifest, write_run_manifest


def sample_data():
    return {"best": {"candidate_id": "candidate-000001", "parameters": {"m1_w": 10e-6}, "objective": 0.1,
                     "specs": {"gain": {"passed": True, "violation": 0.0}},
                     "metrics": {"measured": {"gain_db": 60.0}, "derived": {"gm/id": 12.0},
                                 "unavailable": {"phase_margin_deg": "requires STB"}}},
            "pvt": {"overall_passed": True, "worst": {"point_id": "ss-id", "objective": 0.2},
                    "worst_by_spec": {}, "failures": []}, "failures": [],
            "artifacts": {"best": "candidates/candidate-000001/result.json"}}


def test_run_and_result_manifests_have_distinct_truthful_names(tmp_path):
    run = write_run_manifest(tmp_path, {"config": {"seed": 7}, "artifacts": {"history": "search_history.json"}})
    result = write_result_manifest(tmp_path, sample_data())
    assert run.name == "run_manifest.json" and result.name == "result_manifest.json"
    assert "publishable" not in json.loads(run.read_text(encoding="utf-8"))
    assert json.loads(result.read_text(encoding="utf-8"))["publishable"] is True
    assert not result.read_bytes().startswith(b"\xef\xbb\xbf") and result.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("change", ["empty_specs", "spec", "pvt", "failure"])
def test_publishable_requires_all_gates(tmp_path, change):
    data = sample_data()
    if change == "empty_specs": data["best"]["specs"] = {}
    elif change == "spec": data["best"]["specs"]["gain"]["passed"] = False
    elif change == "pvt": data["pvt"]["overall_passed"] = False
    else: data["failures"].append({"category": "artifact", "message": "blocked", "blocking": True})
    assert json.loads(write_result_manifest(tmp_path, data).read_text(encoding="utf-8"))["publishable"] is False


def test_write_pvt_results_contains_complete_summary(tmp_path):
    points = build_pvt_points(PvtConfig(("tt",), (1.8,), (25.0,)))
    rows = [{"point_id": points[0].point_id, "corner": "tt", "voltage": 1.8, "temperature": 25.0,
             "parameters": {"w": 1e-6}, "metrics": {"gain": 60.0}, "success": True, "objective": 0.0,
             "specs": {"gain": {"passed": True, "violation": 0.0}}, "failure": None}]
    parsed = json.loads(write_pvt_results(tmp_path, summarize_pvt(points, rows)).read_text(encoding="utf-8"))
    assert parsed["overall_passed"] is True and parsed["points"][0]["metrics"]["gain"] == 60.0
    assert "worst" in parsed and "failures" in parsed


def test_markdown_escapes_html_and_table_metacharacters(tmp_path):
    data = sample_data(); data["best"]["metrics"]["measured"]["gain|<&>"] = 61.0
    data["failures"].append({"category": "parse", "message": "bad|<&>", "blocking": False})
    text = write_report(tmp_path, data).read_text(encoding="utf-8")
    assert "gain\\|&lt;&amp;&gt;" in text and "bad\\|&lt;&amp;&gt;" in text
    assert all(h in text for h in ("## Measured Metrics", "## Derived Metrics", "## Unavailable Metrics"))


@pytest.mark.parametrize("bad", ["", ".", "/tmp/x", "C:/abs/x", "C:relative/x", "//server/share/x", r"\\?\C:\x", "../x"])
def test_artifact_paths_reject_cross_platform_escape(tmp_path, bad):
    data = sample_data(); data["artifacts"]["best"] = bad
    with pytest.raises(ValueError): write_result_manifest(tmp_path, data)


def test_artifact_paths_reject_existing_symlink_escape(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside"); outside.mkdir()
    link = tmp_path / "link"
    try: os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError): pytest.skip("symlink unavailable")
    data = sample_data(); data["artifacts"]["best"] = "link/result.json"
    with pytest.raises(ValueError): write_result_manifest(tmp_path, data)


def test_replace_error_has_report_context(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("locked")))
    with pytest.raises(ReportError, match="result_manifest.json.*locked"):
        write_result_manifest(tmp_path, sample_data())

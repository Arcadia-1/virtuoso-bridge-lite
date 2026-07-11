import json
import math

import pytest

from analog_opt.report import write_report, write_run_manifest


def sample_data():
    return {"best": {"candidate_id": "candidate-000001", "parameters": {"m1_w": 10e-6},
                     "objective": 0.1, "specs": {"gain": {"passed": True, "violation": 0.0}},
                     "metrics": {"measured": {"gain_db": 60.0},
                                 "derived": {"op.M1.gm_over_id": 12.0},
                                 "unavailable": {"phase_margin_deg": "requires STB"}}},
            "pvt": {"overall_passed": True, "worst": {"point_id": "ss-v1p62-t125", "objective": 0.2},
                    "worst_by_spec": {}, "failures": []},
            "failures": [], "artifacts": {"best": "candidates/candidate-000001/result.json"}}


def test_manifest_is_deterministic_publishable_and_utf8(tmp_path):
    data = sample_data()
    raw = write_run_manifest(tmp_path, data).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and raw.endswith(b"\n")
    assert json.loads(raw.decode("utf-8"))["publishable"] is True
    assert raw == write_run_manifest(tmp_path, data).read_bytes()


@pytest.mark.parametrize("change", ["spec", "pvt", "failure"])
def test_publishable_requires_all_gates(tmp_path, change):
    data = sample_data()
    if change == "spec":
        data["best"]["specs"]["gain"]["passed"] = False
    elif change == "pvt":
        data["pvt"]["overall_passed"] = False
    else:
        data["failures"].append({"category": "artifact", "message": "blocked", "blocking": True})
    assert json.loads(write_run_manifest(tmp_path, data).read_text(encoding="utf-8"))["publishable"] is False


def test_markdown_separates_metric_classes_and_escapes_tables(tmp_path):
    data = sample_data()
    data["best"]["metrics"]["measured"]["gain|db"] = 61.0
    data["failures"].append({"category": "parse", "message": "bad|row", "blocking": False})
    text = write_report(tmp_path, data).read_text(encoding="utf-8")
    assert all(h in text for h in ("## Measured Metrics", "## Derived Metrics", "## Unavailable Metrics"))
    assert "gain\\|db" in text and "bad\\|row" in text
    assert "phase_margin_deg | 0" not in text


def test_reports_reject_nonfinite_and_path_escape(tmp_path):
    data = sample_data()
    data["best"]["objective"] = math.nan
    with pytest.raises(ValueError):
        write_run_manifest(tmp_path, data)
    data = sample_data()
    data["artifacts"]["best"] = "../outside.json"
    with pytest.raises(ValueError):
        write_report(tmp_path, data)

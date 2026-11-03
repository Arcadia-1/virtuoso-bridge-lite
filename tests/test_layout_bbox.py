"""Offline protocol/analysis/generation checks; not a model of native OA."""

import json
import math

import pytest

from virtuoso_bridge.virtuoso.layout import (
    layout_diagnose_bbox, layout_recompute_bbox,
    parse_layout_bbox_output, parse_layout_bbox_recompute_output,
)


DATA = ('("VB_LAYOUT_BBOX_V1" ((0 0) (100 101)) ((0 0) (10 10)) ("guides") '
        '(("shapes" "rect" nil ((0 0) (10 10)) ("M1" "drawing")) '
        '("steiners" "steiner" nil ((100 101) (100 101)) nil)))')


def test_diagnosis_is_read_only_and_reports_optional_collection_failures():
    code = layout_diagnose_bbox('L"\\', "C", view="layout_V1")
    assert '"L\\"\\\\" "C" "layout_V1"' in code
    assert "unwindProtect" in code and "when(ownsCv" in code
    for collection in ("steiners", "markers", "guides", "routes", "figGroups"):
        assert f'errset(cv~>{collection} nil)' in code
        assert f"member('{collection} cv~>?)" in code
    for forbidden in ("dbComputeBBox", "dbSave", "dbDelete", "lceClear", "geGet"):
        assert forbidden not in code


def test_default_and_dry_run_are_read_only():
    assert layout_recompute_bbox("L", "C") == layout_diagnose_bbox("L", "C")
    with pytest.raises(ValueError):
        layout_recompute_bbox("L", "C", save=True)
    with pytest.raises(ValueError):
        layout_recompute_bbox("L", "C", dry_run="no")


def test_recompute_refuses_unsaved_edits_and_borrows_views_without_promotion():
    code = layout_recompute_bbox("L", "C", dry_run=False)
    assert code.index("dbIsCellViewModified") < code.index("dbComputeBBox")
    assert '"" "a"' in code
    assert "dbReopen" not in code and "dbSave" not in code
    assert "when(ownsCv && cv" in code and "unwindProtect" in code


def test_save_and_verification_fail_explicitly_no_retries():
    code = layout_recompute_bbox("L", "C", dry_run=False, save=True)
    assert 'unless(dbSave(cv)' in code
    assert code.index("dbSave") < code.index('"" "r"')
    assert 'verification = "reopened"' in code and 'verification = "session"' in code
    assert "dbDelete" not in code and "lceClear" not in code
    assert code.count("dbComputeBBox(") == 1 and code.count("dbSave(") == 1


def test_analysis_separates_real_geometry_and_logical_outlier():
    report = parse_layout_bbox_output(DATA, expected_bbox=((0, 0), (10, 10)))
    assert report["geometry_bbox"] == [[0., 0.], [10., 10.]]
    assert report["observed_bbox"] == [[0., 0.], [100., 101.]]
    assert report["bbox_matches_observed"] is True
    assert report["objects"][1]["outside_expected"] is True
    assert report["objects"][1]["cached_edges"] == ["right", "top"]
    assert report["unavailable_collections"] == ["guides"]
    assert parse_layout_bbox_output(json.dumps(DATA)) == parse_layout_bbox_output(DATA)


def test_mismatch_is_not_automatically_corruption_or_deletion_advice():
    report = parse_layout_bbox_output(DATA.replace('(100 101)) ((0', '(200 201)) ((0', 1))
    assert report["bbox_matches_observed"] is False
    assert not any(obj["outside_expected"] for obj in report["objects"])
    assert "not proof of corruption" in report["warnings"][0]


def test_empty_cell_and_negative_scientific_coordinates():
    report = parse_layout_bbox_output('("VB_LAYOUT_BBOX_V1" nil nil nil nil)')
    assert report["bbox"] is None and report["objects"] == []
    report = parse_layout_bbox_output(DATA.replace("100 101", "1e2 1.01e2"))
    assert report["bbox"] == [[0., 0.], [100., 101.]]


@pytest.mark.parametrize("tol", [-1, math.inf, math.nan, True])
def test_invalid_tolerance(tol):
    with pytest.raises(ValueError):
        parse_layout_bbox_output(DATA, tolerance=tol)


@pytest.mark.parametrize("bad", ['"ERROR: missing"', '("bad")', DATA[:-1], DATA + " extra"])
def test_bad_protocol(bad):
    with pytest.raises(ValueError):
        parse_layout_bbox_output(bad)


@pytest.mark.parametrize("box", [((2, 0), (1, 2)), ((0, 0), (math.inf, 2)), ((0,), (1, 2))])
def test_invalid_expected_bbox(box):
    with pytest.raises(ValueError):
        parse_layout_bbox_output(DATA, expected_bbox=box)


@pytest.mark.parametrize("saved,verification", [("nil", "memory_only"), ("t", "session"), ("t", "reopened")])
def test_recompute_evidence(saved, verification):
    raw = f'("VB_BBOX_RECOMPUTE_V1" ((0 0) (10 10)) ((0 0) (1 1)) {saved} "{verification}")'
    report = parse_layout_bbox_recompute_output(raw)
    assert report["changed"] is True
    assert report["verification"] == verification
    assert report["saved"] == (saved == "t")


def test_inconsistent_evidence_rejected():
    with pytest.raises(ValueError):
        parse_layout_bbox_recompute_output('("VB_BBOX_RECOMPUTE_V1" nil nil nil "reopened")')


def test_inverted_database_boxes_are_reported_not_hidden_by_parser_failure():
    report = parse_layout_bbox_output(
        '("VB_LAYOUT_BBOX_V1" ((5 0) (1 1)) nil nil '
        '(("instances" "inst" "BROKEN" ((10 10) (0 0)) nil)))')
    assert report["bbox"] is None and report["observed_bbox"] is None
    assert [issue["location"] for issue in report["invalid_bboxes"]] == ["cellview", "objects[0]"]
    assert report["invalid_bboxes"][1]["raw_bbox"] == [["10", "10"], ["0", "0"]]

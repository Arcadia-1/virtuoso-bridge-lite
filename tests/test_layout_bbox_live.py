"""Native BBox checks in an isolated headless Virtuoso and private library.

On Linux opt in with VB_RUN_LAYOUT_BBOX_TESTS=1 and VB_TEST_VIRTUOSO.
"""

import json
import os
import sys

import pytest

from virtuoso_bridge.virtuoso.layout import (
    layout_diagnose_bbox, layout_recompute_bbox,
    parse_layout_bbox_output, parse_layout_bbox_recompute_output,
)

pytest_plugins = ["tests.test_daemon_lifecycle_live"]
pytestmark = pytest.mark.skipif(
    os.getenv("VB_RUN_LAYOUT_BBOX_TESTS") != "1" or sys.platform != "linux",
    reason="opt-in isolated Linux Cadence session required",
)


def exercise_bbox(evaluate, library_path):
    assert evaluate(f'ddCreateLib("VB_BBOX_TEST" {json.dumps(str(library_path))})') != "nil"
    lib, cell, view = "VB_BBOX_TEST", "extents", "layout_V1"
    names = f'"{lib}" "{cell}" "{view}"'
    find = (f'car(setof(c dbGetOpenCellViews() c~>libName == "{lib}" '
            f'&& c~>cellName == "{cell}" && c~>viewName == "{view}"))')
    evaluate(f'cv = dbOpenCellViewByType({names} "maskLayout" "w")')
    evaluate('r = dbCreateRect(cv list("text" "drawing") list(0:0 10:10))')
    evaluate('s = dbCreateSteiner(cv list(100:100 101:101))')
    evaluate('g = dbCreateGuide(cv 102:102 103:103)')
    evaluate('dbSave(cv)')
    diagnose = layout_diagnose_bbox(lib, cell, view=view)
    report = parse_layout_bbox_output(evaluate(diagnose), expected_bbox=((0, 0), (10, 10)))
    assert report["geometry_bbox"] == [[0., 0.], [10., 10.]], report
    assert any(o["collection"] == "steiners" and o["outside_expected"] for o in report["objects"]), report
    assert any(o["collection"] == "guides" for o in report["objects"]), report
    assert evaluate('dbIsCellViewModified(cv)') == "nil"
    evaluate('extra = dbCreateRect(cv list("text" "drawing") list(20:20 21:21))')
    operation = layout_recompute_bbox(lib, cell, view=view, dry_run=False, save=True)
    assert evaluate('errset(' + operation + ' nil)') == "nil"
    assert evaluate('dbIsId(extra)') == "t"
    evaluate('dbDeleteObject(extra) dbSave(cv)')
    borrowed = parse_layout_bbox_recompute_output(evaluate(operation))
    assert borrowed["verification"] == "session" and borrowed["saved"]
    assert evaluate('dbIsId(s) && dbIsId(g)') == "t"
    assert evaluate(find + ' == cv') == "t"
    evaluate('dbClose(cv)')
    assert evaluate(find) == "nil"
    # The default is exactly a read-only dry-run and leaves no open reference.
    parse_layout_bbox_output(evaluate(layout_recompute_bbox(lib, cell, view=view)))
    assert evaluate(find) == "nil"
    transient = parse_layout_bbox_recompute_output(evaluate(layout_recompute_bbox(
        lib, cell, view=view, dry_run=False)))
    assert transient["saved"] is False and transient["verification"] == "memory_only"
    assert evaluate(find) == "nil"
    saved = parse_layout_bbox_recompute_output(evaluate(operation))
    assert saved["verification"] == "reopened" and saved["saved"]
    assert evaluate(find) == "nil"
    # Recompute does not remove real extents caused by logical objects.
    still_present = parse_layout_bbox_output(evaluate(diagnose))
    assert any(o["collection"] == "steiners" for o in still_present["objects"])
    evaluate(f'cv = dbOpenCellViewByType({names} "" "r")')
    assert evaluate('errset(' + operation + ' nil)') == "nil"
    assert evaluate(find + ' == cv') == "t"
    evaluate('dbClose(cv)')
    # Failure after opening releases only the owned reference. No retries.
    failing = operation.replace('dbComputeBBox(cv)', 'error("injected recompute failure")')
    assert evaluate('errset(' + failing + ' nil)') == "nil"
    assert evaluate(find) == "nil"
    failing = operation.replace('dbSave(cv)', 'nil')
    assert evaluate('errset(' + failing + ' nil)') == "nil"
    assert evaluate(find) == "nil"
    missing = layout_recompute_bbox(lib, "MISSING", dry_run=False, save=True)
    assert evaluate('errset(' + missing + ' nil)') == "nil"
    assert evaluate(f'ddGetObj("{lib}" "MISSING" "layout")') == "nil"


def test_native_bbox(sessions, tmp_path):
    exercise_bbox(sessions("bbox")["evaluate"], tmp_path / "library")

"""Opt-in two-process OA regressions; only private libraries and plain geometry.

On a Linux Cadence host: set VB_RUN_FRESHNESS_TESTS=1 and VB_TEST_VIRTUOSO.
No existing bridge/CIW, PDK, display, or profile configuration is used.
"""

import json
import os
import re
import sys

import pytest

from tests.test_daemon_lifecycle_live import sessions
from virtuoso_bridge.virtuoso.layout import layout_read_geometry, layout_read_summary
from virtuoso_bridge.virtuoso.layout.reader import parse_layout_geometry_output

pytestmark = pytest.mark.skipif(
    os.getenv("VB_RUN_FRESHNESS_TESTS") != "1" or sys.platform != "linux",
    reason="set VB_RUN_FRESHNESS_TESTS=1 and VB_TEST_VIRTUOSO on a Cadence host",
)

BUILDERS = [layout_read_geometry, layout_read_summary]
LIB = "VB_FRESHNESS_TEST"


@pytest.fixture(params=[False, True], ids=["installed-api", "missing-by-name-api"])
def readers(sessions, tmp_path, request):
    writer = sessions("writer")["evaluate"]
    reader = sessions("reader")["evaluate"]
    library_path = json.dumps(str(tmp_path / "library"))
    for evaluate in (writer, reader):
        assert evaluate(f'ddCreateLib("{LIB}" {library_path})') != "nil"
    if request.param:
        # System functions can be write-protected. Rename only this test's
        # generated lookup to an undefined symbol, including its capability
        # probe, to exercise the missing-API path in the native interpreter.
        assert reader("isCallable('vbTestAbsentFindOpenByName)") == "nil"
        original = reader
        reader = lambda code: original(code.replace(
            "dbFindOpenCellViewByName", "vbTestAbsentFindOpenByName"
        ))
    return reader, writer


def _find(cell):
    # Independent oracle: do not reuse either lookup API under test.
    return (
        'car(setof(cv dbGetOpenCellViews() '
        f'cv~>libName == "{LIB}" && cv~>cellName == "{cell}" && cv~>viewName == "layout"))'
    )


def _save_rectangle(evaluate, cell, *, create=False):
    mode = "w" if create else "a"
    return evaluate(
        'let((cv) '
        f'cv = dbOpenCellViewByType("{LIB}" "{cell}" "layout" "maskLayout" "{mode}") '
        'unless(cv error("test open failed")) '
        'unwindProtect(progn('
        'unless(dbCreateRect(cv list("text" "drawing") list(0:0 1:1)) '
        'error("test rectangle failed")) '
        'unless(dbSave(cv) error("test save failed")) length(cv~>shapes)) dbClose(cv)))'
    )


def _count(evaluate, build, cell):
    raw = evaluate(build(LIB, cell))
    if build is layout_read_geometry:
        return sum(obj["kind"] == "shape" for obj in parse_layout_geometry_output(raw))
    match = re.search(r"\((\d+) shapes", raw)
    assert match, raw
    return int(match.group(1))


@pytest.mark.parametrize("build", BUILDERS)
def test_named_read_sees_external_saves_without_accumulating_handles(readers, build):
    reader, writer = readers
    cell = "external_save"
    assert _save_rectangle(writer, cell, create=True) == "1"
    assert _count(reader, build, cell) == 1
    for expected in (2, 3):
        assert _save_rectangle(writer, cell) == str(expected)
        for _ in range(3):
            assert _count(reader, build, cell) == expected
    assert reader(_find(cell)) == "nil"


@pytest.mark.parametrize("build", BUILDERS)
def test_caller_held_snapshot_is_not_forcibly_refreshed(readers, build):
    reader, writer = readers
    cell = "held_snapshot"
    _save_rectangle(writer, cell, create=True)
    reader(f'vbHeld = dbOpenCellViewByType("{LIB}" "{cell}" "layout" "maskLayout" "r")')
    assert _save_rectangle(writer, cell) == "2"
    assert _count(reader, build, cell) == 1
    assert reader(f"{_find(cell)} == vbHeld") == "t"
    assert reader("dbClose(vbHeld)") == "t"
    assert reader(_find(cell)) == "nil"
    assert _count(reader, build, cell) == 2


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("mode", ["r", "a"])
def test_existing_view_and_unsaved_edits_remain_owned_by_caller(readers, build, mode):
    reader, writer = readers
    cell = "caller_owned"
    _save_rectangle(writer, cell, create=True)
    reader(f'vbHeld = dbOpenCellViewByType("{LIB}" "{cell}" "layout" "maskLayout" "{mode}")')
    if mode == "a":
        reader('dbCreateRect(vbHeld list("text" "drawing") list(2:2 3:3))')
    expected = 2 if mode == "a" else 1
    for _ in range(3):
        assert _count(reader, build, cell) == expected
        assert reader(f"{_find(cell)} == vbHeld") == "t"
        assert reader("length(vbHeld~>shapes)") == str(expected)
    # Neither saved our unsaved rectangle nor accumulated extra references.
    assert _count(writer, build, cell) == 1
    assert reader("dbClose(vbHeld)") == "t"
    assert reader(_find(cell)) == "nil"


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("borrowed", [False, True])
def test_read_error_releases_only_an_owned_view(readers, build, borrowed):
    reader, writer = readers
    cell = "error_cleanup"
    _save_rectangle(writer, cell, create=True)
    if borrowed:
        reader(f'vbHeld = dbOpenCellViewByType("{LIB}" "{cell}" "layout" "maskLayout" "r")')
    # Inject a serializer failure after opening: execute real SKILL cleanup,
    # not a Python model of Cadence's ownership/refcount semantics.
    skill = build(LIB, cell)
    assert "foreach(shape cv~>shapes " in skill
    skill = skill.replace("foreach(shape cv~>shapes ",
                          'foreach(shape cv~>shapes error("injected reader failure") ', 1)
    assert reader(f"errset({skill} nil)") == "nil"
    if borrowed:
        assert reader(f"{_find(cell)} == vbHeld") == "t"
        assert reader("dbClose(vbHeld)") == "t"
    assert reader(_find(cell)) == "nil"


@pytest.mark.parametrize("build", BUILDERS)
def test_missing_cell_does_not_leave_an_open_view(readers, build):
    reader, _ = readers
    assert "ERROR: cannot open" in reader(build(LIB, "does_not_exist"))
    assert reader(_find("does_not_exist")) == "nil"


@pytest.mark.parametrize("build", BUILDERS)
def test_missing_library_does_not_call_lookup_with_nil_library(readers, build):
    reader, _ = readers
    assert "ERROR: cannot open" in reader(build("VB_FRESHNESS_ABSENT_LIBRARY", "missing"))

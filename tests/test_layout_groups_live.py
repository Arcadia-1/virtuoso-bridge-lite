"""Native OA coverage in a new headless process, never a user's CIW.

Set VB_RUN_LAYOUT_GROUP_TESTS=1 and VB_TEST_VIRTUOSO on a Cadence Linux host.
"""

import os
import sys

import pytest

pytest_plugins = ["tests.test_daemon_lifecycle_live"]
from virtuoso_bridge.virtuoso.layout import (
    layout_create_fig_group, layout_create_group, layout_add_to_group,
    layout_remove_from_group, layout_transform_fig_group, layout_ungroup,
    layout_read_groups, parse_layout_groups_output, layout_read_geometry,
    parse_layout_geometry_output,
)

pytestmark = pytest.mark.skipif(
    os.getenv("VB_RUN_LAYOUT_GROUP_TESTS") != "1" or sys.platform != "linux",
    reason="opt-in isolated Linux Cadence session required",
)


def exercise_groups(evaluate, library_path):
    """Shared native assertions; all design data stays in a private library."""
    import json

    assert evaluate(f'ddCreateLib("VB_GROUP_TEST" {json.dumps(str(library_path))})') != "nil"
    evaluate('cv = dbOpenCellViewByType("VB_GROUP_TEST" "groups" "layout" "maskLayout" "w")')
    evaluate('r = dbCreateRect(cv list("text" "drawing") list(0:0 1:2))')
    evaluate(layout_create_fig_group("pair"))
    evaluate(layout_add_to_group("pair", "r"))
    evaluate(layout_add_to_group("pair", "r"))
    assert evaluate('length(dbGetFigGroupByName(cv "pair")~>figs)') == "1"
    assert evaluate('errset(' + layout_create_fig_group("pair") + ' nil)') == "nil"
    evaluate(layout_create_fig_group("other"))
    assert evaluate('errset(' + layout_add_to_group("other", "r") + ' nil)') == "nil"
    evaluate(layout_ungroup("other"))
    evaluate('foreignCv = dbOpenCellViewByType("VB_GROUP_TEST" "foreign" "layout" "maskLayout" "w")')
    evaluate('foreignRect = dbCreateRect(foreignCv list("text" "drawing") list(0:0 1:1))')
    assert evaluate('errset(' + layout_add_to_group("pair", "foreignRect") + ' nil)') == "nil"
    evaluate('dbSave(foreignCv) dbClose(foreignCv)')
    evaluate(layout_transform_fig_group("pair", offset=(4, 3), orientation="R90"))
    assert evaluate('r~>bBox') == "((2.0 3.0) (4.0 4.0))"
    evaluate(layout_create_group("logical"))
    evaluate(layout_add_to_group("logical", "r", kind="group"))
    rows = parse_layout_groups_output(evaluate(layout_read_groups("VB_GROUP_TEST", "groups")))
    assert {row["kind"] for row in rows} == {"group", "figGroup"}
    assert all(len(row["members"]) == 1 for row in rows), rows
    assert len(parse_layout_geometry_output(evaluate(layout_read_geometry(
        "VB_GROUP_TEST", "groups", include_groups=True)))) == 3
    evaluate(layout_remove_from_group("pair", "r"))
    evaluate(layout_remove_from_group("pair", "r"))
    assert evaluate('dbIsId(r)') == "t"
    evaluate(layout_add_to_group("pair", "r"))
    evaluate(layout_ungroup("pair"))
    evaluate(layout_ungroup("logical", kind="group"))
    assert evaluate('dbIsId(r)') == "t"
    assert evaluate('length(cv~>shapes)') == "1"
    # OA may reject punctuation even when correctly escaped as SKILL text.
    assert evaluate('errset(' + layout_create_fig_group('A"\\B') + ' nil)') == "nil"
    assert evaluate('dbSave(cv)') == "t"
    assert evaluate('dbClose(cv)') == "t"
    assert parse_layout_groups_output(evaluate(layout_read_groups("VB_GROUP_TEST", "groups"))) == []
    evaluate('cv = dbOpenCellViewByType("VB_GROUP_TEST" "groups" "layout" "maskLayout" "r")')
    assert evaluate('errset(' + layout_create_fig_group("readonly") + ' nil)') == "nil"
    evaluate('dbClose(cv)')


def test_native_groups(sessions, tmp_path):
    exercise_groups(sessions("groups")["evaluate"], tmp_path / "library")

"""Offline generation contracts; native OA freshness is tested separately."""

import pytest

from tests.test_layout_edit_target import _balanced
from virtuoso_bridge.virtuoso.layout import layout_read_geometry, layout_read_summary


@pytest.mark.parametrize("build", [layout_read_geometry, layout_read_summary])
def test_named_layout_read_borrows_or_closes_its_own_view(build):
    skill = build("L", "C")
    find = 'cv = dbFindOpenCellViewByName("L" "C" "layout")'
    assert find in skill
    assert 'unless(cv cv = dbOpenCellViewByType(' in skill
    assert skill.index(find) < skill.index("dbOpenCellViewByType(")
    assert "ownsCv = cv" in skill
    assert "unwindProtect(progn(" in skill
    assert "when(ownsCv unless(dbClose(cv)" in skill
    assert 'error("layout reader close failed")' in skill
    assert "dbPurge" not in skill and "dbSave" not in skill
    assert _balanced(skill)


@pytest.mark.parametrize("build", [layout_read_geometry, layout_read_summary])
def test_named_layout_read_escapes_names_and_keeps_custom_view_type(build):
    skill = build('L"x', "C\\x", view='custom"view', view_type="maskLayout")
    names = '"L\\"x" "C\\\\x" "custom\\"view"'
    assert f"dbFindOpenCellViewByName({names})" in skill
    assert f'dbOpenCellViewByType({names} "maskLayout" "r")' in skill
    assert "ERROR: cannot open %s/%s/%s" in skill
    assert _balanced(skill)

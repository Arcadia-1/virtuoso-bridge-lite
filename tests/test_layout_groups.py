"""Offline generation and parsing tests; no Cadence behavior is simulated."""

import math
import json

import pytest

from virtuoso_bridge.virtuoso.layout import (
    layout_create_fig_group, layout_create_group, layout_add_to_group,
    layout_remove_from_group, layout_transform_fig_group, layout_ungroup,
    layout_read_groups, parse_layout_groups_output, layout_read_geometry,
    parse_layout_geometry_output,
)


def test_create_types_are_distinct_and_never_replace():
    fg = layout_create_fig_group('A"\\B', ordered=True, origin=(2, 3))
    assert 'dbCreateFigGroup(cv "A\\"\\\\B" t list(2 3) "R0")' in fg
    assert fg.index("already exists") < fg.index("dbCreateFigGroup")
    group = layout_create_group("G")
    assert '"set" "unordered" "uniqueName" "deleteNever"' in group
    assert "dbCreateFigGroup" not in group
    for code in (group, fg):
        assert "not editable" in code
        assert "geGet" not in code and "hi" not in code


@pytest.mark.parametrize("name", ["", " ", None, "x\x00y"])
def test_invalid_names(name):
    with pytest.raises(ValueError):
        layout_create_fig_group(name)


@pytest.mark.parametrize("point", [(math.nan, 0), (0, math.inf), (True, 0), (1,)])
def test_invalid_point(point):
    with pytest.raises(ValueError):
        layout_transform_fig_group("G", offset=point)


def test_invalid_orientation_and_kind():
    with pytest.raises(ValueError):
        layout_transform_fig_group("G", orientation='R0") exit()')
    with pytest.raises(ValueError):
        layout_add_to_group("G", "obj", kind="bad")


@pytest.mark.parametrize("kind", ["figGroup", "group"])
def test_membership_guards_and_idempotency(kind):
    add = layout_add_to_group("G", "newRect", kind=kind, cv_expr="target")
    remove = layout_remove_from_group("G", "newRect", kind=kind)
    assert "rbObject = newRect" in add
    assert "rbObject~>cellView == target" in add
    assert "when(!member" in add and "when(member" in remove
    assert "dbDeleteObject(" not in remove
    assert add.index("cross-cellview") < add.index("when(!member")
    if kind == "figGroup":
        assert "another figGroup" in add and "nested figGroup" in add


def test_transform_is_relative_in_place_no_scale_and_no_member_iteration():
    code = layout_transform_fig_group("G", offset=(4, -2), orientation="MY")
    assert 'dbMoveFig(rbGroup nil list(list(4 -2) "MY" 1.0))' in code
    assert "foreach" not in code and "~>origin =" not in code


@pytest.mark.parametrize("kind", ["figGroup", "group"])
def test_ungroup_never_deletes_figures(kind):
    code = layout_ungroup("G", kind=kind)
    assert "dbDeleteObject(rbGroup)" in code
    assert "dbDeleteGroupByName" not in code and "foreach" not in code


def test_group_reader_owns_only_its_reference():
    code = layout_read_groups('L"', "C")
    assert 'ddGetObj("L\\"")' in code
    assert "unwindProtect" in code and "when(ownsCv" in code
    assert '"r"' in code and "dbSave" not in code
    assert "g~>figs" in code and "g~>members~>object" in code


DATA = '(("figGroup" "pair" ((-1e-3 0) (2 3)) t (("rect" nil ((0 0) (1 1)) ("M1" "drawing")))) ("group" "logic" nil nil nil))'


def test_parse_groups_and_optional_geometry_readback():
    data = parse_layout_groups_output(DATA)
    assert data[0]["bbox"] == [[-.001, 0.], [2., 3.]]
    assert data[0]["members"][0]["lpp"] == ["M1", "drawing"]
    assert data[1] == dict(kind="group", name="logic", bbox=None, ordered=False, members=[])
    assert parse_layout_geometry_output("groups\tdata=" + DATA) == data
    assert "cv~>figGroups" not in layout_read_geometry("L", "C")
    assert "cv~>figGroups" in layout_read_geometry("L", "C", include_groups=True)
    assert parse_layout_groups_output('"nil"') == []


def test_group_names_with_literal_escapes_survive_geometry_transport():
    payload = '(("figGroup" "A\\nB\\tC\\\\D\\\"E" nil nil nil))'
    expected = parse_layout_groups_output(payload)
    assert expected[0]["name"] == 'A\nB\tC\\D"E'
    assert parse_layout_geometry_output('groups\tdata=' + payload) == expected
    assert parse_layout_geometry_output(json.dumps('groups\tdata=' + payload + '\n')) == expected


@pytest.mark.parametrize("value", ['"ERROR: cannot open"', '("bad")', '(("figGroup" "g" nil t (("rect"))))'])
def test_malformed_output_fails(value):
    with pytest.raises(ValueError):
        parse_layout_groups_output(value)

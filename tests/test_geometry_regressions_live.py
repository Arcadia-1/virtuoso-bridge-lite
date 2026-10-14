"""Public-helper regressions for #242/#243; dedicated session only.

Uses the opt-in client/library fixtures and cleanup from test_bridge_live.
Only analogLib and a generic text/drawing rectangle are used, never a PDK.
"""
from __future__ import annotations

import os

import pytest

from tests.test_bridge_live import client, lib, _cell, _ok
from virtuoso_bridge.virtuoso.schematic import (
    schematic_create_inst_by_master_name,
    schematic_create_wire,
    schematic_label_instance_term_offset,
)
from virtuoso_bridge.virtuoso.layout import (
    layout_create_rect,
    layout_create_simple_mosaic,
    layout_create_param_inst,
    layout_read_geometry,
)
from virtuoso_bridge.virtuoso.layout.reader import (
    _parse_skill_point_list,
    parse_layout_geometry_output,
)

pytestmark = pytest.mark.skipif(os.getenv("VB_RUN_LIVE_TESTS") != "1",
                              reason="dedicated live Virtuoso session required")


@pytest.mark.parametrize("direction", ["right", "left", "up", "down"])
def test_offset_label_binds_the_terminal_net(client, lib, direction):
    cell = _cell("offset_" + direction)
    with client.schematic.create(lib, cell) as sch:
        sch.add(schematic_create_inst_by_master_name(
            "analogLib", "cap", "symbol", "C0", 0, 0, "R0"))
        sch.add(schematic_label_instance_term_offset(
            "C0", "PLUS", "INTENDED", branch_direction=direction))
    data = client.schematic.read(lib, cell)
    cap = next(i for i in data["instances"] if i["name"] == "C0")
    assert cap["terms"]["PLUS"] == "INTENDED", cap["terms"]


def test_generic_wire_preserves_every_supplied_vertex(client, lib):
    cell = _cell("polyline")
    points = [(0., 0.), (1., 0.), (1., 1.), (2., 1.)]
    with client.schematic.create(lib, cell) as sch:
        sch.add(schematic_create_wire(iter(points)))
    raw = _ok(client.execute_skill(
        f'let((cv) cv = dbOpenCellViewByType("{lib}" "{cell}" "schematic" nil "r") '
        'cv~>shapes~>points)'))
    actual = _parse_skill_point_list(raw)
    assert actual and all(point in actual for point in points), raw


def test_geometry_reads_mosaic_and_ordinary_instance(client, lib):
    leaf, parent = _cell("leaf"), _cell("array")
    with client.layout.create(lib, leaf) as lay:
        lay.add(layout_create_rect("text", "drawing", 0, 0, 1, 1))
    with client.layout.create(lib, parent) as lay:
        lay.add(layout_create_param_inst(lib, leaf, "layout", "ONE", 10, 10, "R0"))
        lay.add(layout_create_simple_mosaic(lib, leaf, rows=2, cols=3,
                                           row_pitch=2, col_pitch=2, instance_name="ARR"))
    raw = _ok(client.execute_skill(layout_read_geometry(lib, parent)))
    objects = parse_layout_geometry_output(raw)
    arrays = [obj for obj in objects if obj["kind"] == "mosaic"]
    assert len(arrays) == 1, objects
    assert (arrays[0]["name"], arrays[0]["rows"], arrays[0]["columns"]) == ("ARR", 2, 3)
    assert arrays[0]["bbox"] == [(0., 0.), (5., 3.)], arrays[0]
    assert [obj["name"] for obj in objects if obj["kind"] == "instance"] == ["ONE"]

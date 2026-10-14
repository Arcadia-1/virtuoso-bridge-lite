"""Offline contracts for #242/#243; native coverage is separately opt-in."""
from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.schematic import schematic_create_wire, schematic_label_instance_term_offset
from virtuoso_bridge.virtuoso.layout import layout_read_geometry
from virtuoso_bridge.virtuoso.layout.reader import parse_layout_geometry_output


def test_default_polyline_uses_every_vertex_not_two_point_route():
    skill = schematic_create_wire(iter([(0, 0), (1, 0), (1, 1)]))
    assert '"draw"' in skill
    assert '(1.000 1.000)' in skill


def test_explicit_route_rejects_silently_ignored_points():
    with pytest.raises(ValueError, match="two points"):
        schematic_create_wire([(0, 0), (1, 0), (1, 1)], route_style="route")


def test_two_point_default_and_explicit_draw_keep_existing_behavior():
    assert '"route" "full"' in schematic_create_wire([(0, 0), (1, 1)])
    assert '"draw"' in schematic_create_wire([(0, 0), (1, 1)], route_style="draw")


def test_implicit_polyline_does_not_silently_override_flight_mode():
    with pytest.raises(ValueError, match="route_mode"):
        schematic_create_wire([(0, 0), (1, 0), (1, 1)], route_mode="flight")
    assert '"route" "flight"' in schematic_create_wire([(0, 0), (1, 1)], route_mode="flight")


def test_explicit_style_is_not_replaced_by_default_selection():
    assert '"" "full"' in schematic_create_wire([(0, 0), (1, 1)], route_style="")


def test_offset_polyline_and_label_share_explicit_cellview():
    skill = schematic_label_instance_term_offset("C0", "PLUS", "INTENDED", cv_expr="targetCv")
    assert 'schCreateWire(targetCv "draw" "full" list(rbCtr rbStubEnd rbBranchEnd)' in skill
    assert 'schCreateWireLabel(targetCv ' in skill


def test_layout_uses_native_aggregate_bbox_and_serializes_arrays():
    skill = layout_read_geometry("L", "C")
    assert 'mosaic\\t' in skill
    assert 'rows=' in skill and 'columns=' in skill
    assert 'dbTransformBBox(inst~>master~>bBox inst~>transform)' not in skill


def test_modeled_mosaic_row_counts_are_numeric_not_ordinary_instances():
    # Modeled from the issue's native readback, not a recorded Cadence fixture.
    data = parse_layout_geometry_output(
        'mosaic\tname=ARR\trows=2\tcolumns=3\tbbox=((0 0) (5 3))\txy=(0 0)\n')
    assert data[0]["kind"] == "mosaic"
    assert (data[0]["rows"], data[0]["columns"]) == (2, 3)
    assert data[0]["bbox"] == [(0., 0.), (5., 3.)]

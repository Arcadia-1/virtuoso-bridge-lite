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
    from tests.test_layout_edit_target import _balanced
    from virtuoso_bridge.virtuoso.layout import layout_read_summary

    skill = layout_read_geometry("L", "C")
    assert _balanced(skill)
    assert 'mosaic\\t' in skill
    assert 'rows=' in skill and 'columns=' in skill
    assert 'dbTransformBBox(inst~>master~>bBox inst~>transform)' not in skill
    assert "(or inst~>libName inst~>master~>libName)" in skill
    assert "(or inst~>cellName inst~>master~>cellName)" in skill
    assert "(or inst~>viewName inst~>master~>viewName)" in skill
    assert "(or inst~>orient car(inst~>tileArray))" in skill

    summary = layout_read_summary("L", "C")
    assert _balanced(summary)
    assert '(or inst~>name "")' in summary
    assert '(or inst~>libName inst~>master~>libName "?")' in summary
    assert '(or inst~>cellName inst~>master~>cellName "?")' in summary


def test_modeled_mosaic_row_counts_are_numeric_not_ordinary_instances():
    # Modeled from the issue's native readback, not a recorded Cadence fixture.
    data = parse_layout_geometry_output(
        'mosaic\tname=ARR\trows=2\tcolumns=3\tbbox=((0 0) (5 3))\txy=(0 0)\n')
    assert data[0]["kind"] == "mosaic"
    assert (data[0]["rows"], data[0]["columns"]) == (2, 3)
    assert data[0]["bbox"] == [(0., 0.), (5., 3.)]


def test_parse_layout_geometry_handles_scientific_notation_coordinates():
    raw = (
        'shape\tobjType=path\tlayer=M1\tpurpose=drawing\t'
        'bbox=((1e-05 0.0) (1.0 2.0))\t'
        'points=((0 0) (1e-05 2e-05) (1 1))\t'
        'xy=(1e-05 2.0)\torient="R0"\ttext=nil\n'
        'shape\tobjType=label\tlayer=M1\tpurpose=pin\t'
        'bbox=((-1.5E+06 +.25) (1. 2.e-3))\tpoints=nil\t'
        'xy=(-1.5E+06 +.25)\torient="R0"\ttext="VDD"\n'
    )
    objects = parse_layout_geometry_output(raw)
    assert len(objects) == 2
    assert objects[0]["xy"] == (1e-05, 2.0)
    assert objects[0]["bbox"] == [(1e-05, 0.0), (1.0, 2.0)]
    assert objects[0]["points"] == [(0.0, 0.0), (1e-05, 2e-05), (1.0, 1.0)]
    assert objects[1]["xy"] == (-1.5e6, 0.25)
    assert objects[1]["bbox"] == [(-1.5e6, 0.25), (1.0, 0.002)]

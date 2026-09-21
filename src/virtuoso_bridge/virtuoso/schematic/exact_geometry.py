"""Exact-coordinate schematic geometry adaptation.

This module preserves an existing drawing instead of inventing a new layout.
Source instance origins, endpoint coordinates, bends, junctions, and repeated
port occurrences are transformed into a target symbol library.  Target master
pin offsets are explicit inputs, so a PDK change moves instance origins while
keeping every source horizontal/vertical relationship exact.

The implementation is pure Python.  It can be validated without a Cadence
installation before any database operation is sent to Virtuoso.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence


Point = tuple[float, float]
Expression = tuple[str, Point]


_ORIENTATION_BY_SOURCE = {
    (0, "none"): "R0",
    (0, "horizontal"): "MY",
    (0, "vertical"): "MX",
    (0, "both"): "R180",
    (90, "none"): "R270",
    (90, "horizontal"): "MYR90",
    (90, "vertical"): "MXR90",
    (90, "both"): "R90",
    (180, "none"): "R180",
    (180, "horizontal"): "MX",
    (180, "vertical"): "MY",
    (180, "both"): "R0",
    (270, "none"): "R90",
    (270, "horizontal"): "MXR90",
    (270, "vertical"): "MYR90",
    (270, "both"): "R270",
}


@dataclass(frozen=True)
class ExactGeometryConfig:
    """Coordinate conversion settings for an exact schematic drawing."""

    source_scale: float = 1.0
    origin: Point = (0.0, 0.0)
    local_label_stub_length: float = 4.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.source_scale) or self.source_scale <= 0:
            raise ValueError("source_scale must be a positive finite number")
        if len(self.origin) != 2 or not all(math.isfinite(float(v)) for v in self.origin):
            raise ValueError("origin must contain two finite coordinates")
        if (
            not math.isfinite(self.local_label_stub_length)
            or self.local_label_stub_length <= 0
        ):
            raise ValueError("local_label_stub_length must be positive")


def source_orientation(transform: Mapping[str, Any]) -> str:
    """Translate a source rotation/mirror pair to a Cadence orientation."""

    key = (
        int(transform.get("rotation", 0)) % 360,
        str(transform.get("mirror", "none")),
    )
    try:
        return _ORIENTATION_BY_SOURCE[key]
    except KeyError as exc:
        raise ValueError(f"unsupported source transform {key!r}") from exc


def orient_offset(offset: Sequence[float], orientation: str) -> Point:
    """Apply a Cadence orientation to a master-local pin offset."""

    x, y = float(offset[0]), float(offset[1])
    transforms = {
        "R0": (x, y),
        "R90": (-y, x),
        "R180": (-x, -y),
        "R270": (y, -x),
        "MY": (-x, y),
        "MX": (x, -y),
        "MXR90": (y, x),
        "MYR90": (-y, -x),
    }
    try:
        return transforms[orientation]
    except KeyError as exc:
        raise ValueError(f"unsupported Cadence orientation {orientation!r}") from exc


def pin_orientation(port: Mapping[str, Any]) -> str:
    """Resolve the visible orientation for a source port occurrence."""

    if "targetOrient" in port:
        return str(port["targetOrient"])
    symbol = port.get("sourceSymbolId")
    if symbol == "vdd-port":
        return "R270"
    if symbol == "ground":
        return "R90"
    orientation = source_orientation(port.get("sourceTransform", {}))
    vector = orient_offset((1, 0), orientation)
    return {
        (1.0, 0.0): "R0",
        (0.0, 1.0): "R90",
        (-1.0, 0.0): "R180",
        (0.0, -1.0): "R270",
    }[vector]


def _point(value: Mapping[str, Any]) -> Point:
    return float(value["x"]), float(value["y"])


def _rounded(value: float) -> float:
    rounded = round(value, 9)
    return 0.0 if rounded == -0.0 else rounded


def _instance_offsets(
    item: Mapping[str, Any],
    pin_offsets: Mapping[str, Mapping[str, Sequence[float]]],
) -> Mapping[str, Sequence[float]]:
    master = f'{item["targetLibrary"]}/{item["targetCell"]}'
    if master not in pin_offsets:
        raise ValueError(f"missing pin offsets for target master {master}")
    return pin_offsets[master]


def _instance_anchors(
    item: Mapping[str, Any],
    origin: Point,
    orientation: str,
    pin_offsets: Mapping[str, Mapping[str, Sequence[float]]],
) -> dict[str, Point]:
    result: dict[str, Point] = {}
    for pin_name, offset in _instance_offsets(item, pin_offsets).items():
        dx, dy = orient_offset(offset, orientation)
        result[str(pin_name)] = (_rounded(origin[0] + dx), _rounded(origin[1] + dy))
    return result


def solve_exact_geometry(
    source: Mapping[str, Any],
    pin_offsets: Mapping[str, Mapping[str, Sequence[float]]],
    config: ExactGeometryConfig | None = None,
) -> dict[str, Any]:
    """Resolve a source drawing against target-master pin coordinates.

    The source must provide ``instances`` and ``sourceGeometry``.  Endpoint
    references in routes and contacts may target an instance terminal, a port
    occurrence, or a junction.  Horizontal and vertical source edges become
    hard coordinate equalities.  Diagonal edges retain their source direction
    and are emitted directly by the database workflow.
    """

    settings = config or ExactGeometryConfig()
    geometry = source["sourceGeometry"]
    bounds = geometry["bounds"]

    def convert(value: Mapping[str, Any]) -> Point:
        x, y = _point(value)
        return (
            _rounded(settings.origin[0] + (x - float(bounds["minX"])) * settings.source_scale),
            _rounded(settings.origin[1] + (float(bounds["maxY"]) - y) * settings.source_scale),
        )

    preferred: dict[str, Point] = {}
    orientations: dict[str, str] = {}
    source_endpoint_targets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    instances = source["instances"]
    for item in instances:
        reference = str(item["reference"])
        origin = convert(item["sourcePosition"])
        orientation = source_orientation(item.get("sourceTransform", {}))
        variable = f"instance:{reference}"
        preferred[variable] = origin
        orientations[reference] = orientation
        source_id = str(item.get("sourceExpandedFrom", item["id"]))
        offsets = _instance_offsets(item, pin_offsets)
        for node in item["nodes"]:
            pin_name = str(node["pinName"])
            if pin_name not in offsets:
                master = f'{item["targetLibrary"]}/{item["targetCell"]}'
                raise ValueError(f"{master} has no configured pin offset {pin_name!r}")
            key = (source_id, str(node["sourcePinName"]))
            offset = orient_offset(offsets[pin_name], orientation)
            source_endpoint_targets.setdefault(key, []).append(
                {
                    "expression": (variable, offset),
                    "reference": reference,
                    "pinName": pin_name,
                    "netName": str(node["netName"]),
                }
            )

    ports: list[dict[str, Any]] = []
    port_variables: dict[str, str] = {}
    for original in geometry.get("portOccurrences", []):
        port = dict(original)
        occurrence_id = str(port["occurrenceId"])
        variable = f"port:{occurrence_id}"
        preferred[variable] = convert(port["sourcePosition"])
        port_variables[occurrence_id] = variable
        port["variable"] = variable
        ports.append(port)

    junction_variables: dict[str, str] = {}
    for item in geometry.get("junctions", []):
        junction_id = str(item["id"])
        variable = f"junction:{junction_id}"
        preferred[variable] = convert(item["sourcePosition"])
        junction_variables[junction_id] = variable

    def endpoint_expression(endpoint: Mapping[str, Any]) -> Expression:
        if endpoint["kind"] == "junction":
            return junction_variables[str(endpoint["junctionId"])], (0.0, 0.0)
        instance_id = str(endpoint["instanceId"])
        if instance_id in port_variables:
            return port_variables[instance_id], (0.0, 0.0)
        rows = source_endpoint_targets.get((instance_id, str(endpoint["pinName"])), [])
        if not rows:
            raise ValueError(
                f'{source["cellName"]} cannot resolve source endpoint '
                f'{instance_id}.{endpoint["pinName"]}'
            )
        if len(rows) > 1:
            net_name = endpoint.get("netName")
            matching = [row for row in rows if row["netName"] == net_name]
            if len(matching) == 1:
                return matching[0]["expression"]
        return rows[0]["expression"]

    equations: dict[int, list[tuple[Expression, Expression, dict[str, str]]]] = {
        0: [],
        1: [],
    }
    geometry_edges: list[dict[str, Any]] = []

    def add_exact_edge(
        kind: str,
        edge_id: str,
        source_a: Mapping[str, Any],
        expression_a: Expression,
        source_b: Mapping[str, Any],
        expression_b: Expression,
    ) -> None:
        ax, ay = _point(source_a)
        bx, by = _point(source_b)
        same_x = math.isclose(ax, bx, abs_tol=1e-12)
        same_y = math.isclose(ay, by, abs_tol=1e-12)
        metadata = {"kind": kind, "id": edge_id}
        expected_delta = (
            _rounded((bx - ax) * settings.source_scale),
            _rounded((ay - by) * settings.source_scale),
        )
        if same_x:
            equations[0].append((expression_a, expression_b, metadata))
        if same_y:
            equations[1].append((expression_a, expression_b, metadata))
        geometry_edges.append(
            {
                "kind": kind,
                "id": edge_id,
                "sameX": same_x,
                "sameY": same_y,
                "expectedDelta": expected_delta,
                "expressionA": expression_a,
                "expressionB": expression_b,
            }
        )

    route_rows: list[tuple[Mapping[str, Any], list[Expression]]] = []
    for route in geometry.get("routes", []):
        source_points = [route["start"]["sourcePoint"]]
        expressions = [endpoint_expression(route["start"])]
        for index, step in enumerate(route.get("steps", [])):
            if step["kind"] == "bend":
                variable = f'bend:{route["id"]}:{index}'
                preferred[variable] = convert(step["position"])
                source_points.append(step["position"])
                expressions.append((variable, (0.0, 0.0)))
            else:
                source_points.append(step["sourcePoint"])
                expressions.append(endpoint_expression(step))
        for index, (source_a, source_b, expression_a, expression_b) in enumerate(
            zip(source_points, source_points[1:], expressions, expressions[1:])
        ):
            add_exact_edge(
                "route",
                f'{route["id"]}:{index}',
                source_a,
                expression_a,
                source_b,
                expression_b,
            )
        route_rows.append((route, expressions))

    contact_rows: list[tuple[Mapping[str, Any], list[Expression]]] = []
    for contact in geometry.get("contacts", []):
        expressions = [endpoint_expression(item) for item in contact["endpoints"]]
        source_points = [item["sourcePoint"] for item in contact["endpoints"]]
        for index in range(1, len(expressions)):
            add_exact_edge(
                "contact",
                f'{contact["id"]}:{index}',
                source_points[0],
                expressions[0],
                source_points[index],
                expressions[index],
            )
        contact_rows.append((contact, expressions))

    annotation_rows: list[tuple[Mapping[str, Any], Expression, Expression]] = []
    for item in geometry.get("annotationStubs", []):
        if item.get("anchorJunctionId"):
            start_expression = (
                junction_variables[str(item["anchorJunctionId"])],
                (0.0, 0.0),
            )
        else:
            variable = f'annotation-start:{item["annotationId"]}'
            preferred[variable] = convert(item["start"])
            start_expression = (variable, (0.0, 0.0))
        end_expression = (
            port_variables[str(item["annotationId"])],
            (0.0, 0.0),
        )
        add_exact_edge(
            "annotation",
            str(item["annotationId"]),
            item["start"],
            start_expression,
            item["end"],
            end_expression,
        )
        annotation_rows.append((item, start_expression, end_expression))

    def solve_axis(axis: int) -> tuple[dict[str, float], int]:
        graph: dict[str, list[tuple[str, float, Mapping[str, str]]]] = {
            variable: [] for variable in preferred
        }
        for left, right, metadata in equations[axis]:
            left_variable, left_offset = left
            right_variable, right_offset = right
            delta = left_offset[axis] - right_offset[axis]
            graph[left_variable].append((right_variable, delta, metadata))
            graph[right_variable].append((left_variable, -delta, metadata))

        values: dict[str, float] = {}
        component_count = 0
        for root in sorted(graph):
            if root in values:
                continue
            component_count += 1
            relative = {root: 0.0}
            pending = [root]
            for current in pending:
                for neighbor, delta, metadata in graph[current]:
                    expected = _rounded(relative[current] + delta)
                    if neighbor in relative:
                        if not math.isclose(relative[neighbor], expected, abs_tol=1e-9):
                            axis_name = "X" if axis == 0 else "Y"
                            raise ValueError(
                                f'{source["cellName"]} has inconsistent exact '
                                f"{axis_name} constraints at {metadata}: "
                                f"{relative[neighbor]} != {expected}"
                            )
                        continue
                    relative[neighbor] = expected
                    pending.append(neighbor)
            translation = _rounded(
                sum(preferred[node][axis] - offset for node, offset in relative.items())
                / len(relative)
            )
            for node, offset in relative.items():
                values[node] = _rounded(translation + offset)
        return values, component_count

    solved_x, x_components = solve_axis(0)
    solved_y, y_components = solve_axis(1)
    solved = {name: (solved_x[name], solved_y[name]) for name in preferred}

    def expression_point(expression: Expression, coordinates: Mapping[str, Point]) -> Point:
        variable, offset = expression
        point = coordinates[variable]
        return _rounded(point[0] + offset[0]), _rounded(point[1] + offset[1])

    violations: list[dict[str, Any]] = []
    for edge in geometry_edges:
        first = expression_point(edge["expressionA"], solved)
        second = expression_point(edge["expressionB"], solved)
        axes: list[str] = []
        if edge["sameX"] and not math.isclose(first[0], second[0], abs_tol=1e-9):
            axes.append("X")
        if edge["sameY"] and not math.isclose(first[1], second[1], abs_tol=1e-9):
            axes.append("Y")
        if not edge["sameX"] and not edge["sameY"]:
            actual_delta = (second[0] - first[0], second[1] - first[1])
            expected_delta = edge["expectedDelta"]
            if actual_delta[0] == 0 or (actual_delta[0] > 0) != (expected_delta[0] > 0):
                axes.append("DX")
            if actual_delta[1] == 0 or (actual_delta[1] > 0) != (expected_delta[1] > 0):
                axes.append("DY")
        if axes:
            violations.append(
                {
                    "kind": edge["kind"],
                    "id": edge["id"],
                    "axes": axes,
                    "targetA": first,
                    "targetB": second,
                }
            )
    if violations:
        raise ValueError(
            f'{source["cellName"]} retains {len(violations)} exact coordinate violations'
        )

    placements = {
        str(item["reference"]): solved[f'instance:{item["reference"]}']
        for item in instances
    }
    anchors = {
        str(item["reference"]): _instance_anchors(
            item,
            placements[str(item["reference"])],
            orientations[str(item["reference"])],
            pin_offsets,
        )
        for item in instances
    }
    for port in ports:
        port["xy"] = solved[port.pop("variable")]

    junction_points = {
        str(item["id"]): solved[junction_variables[str(item["id"])]]
        for item in geometry.get("junctions", [])
    }
    bulk_stubs: list[dict[str, Any]] = []
    for label in geometry.get("localBulkLabels", []):
        reference = str(label["reference"])
        anchor = anchors[reference][str(label["pinName"])]
        origin = placements[reference]
        dx, dy = anchor[0] - origin[0], anchor[1] - origin[1]
        if abs(dx) >= abs(dy):
            outward = (1 if dx >= 0 else -1, 0)
        else:
            outward = (0, 1 if dy >= 0 else -1)
        endpoint = (
            _rounded(anchor[0] + settings.local_label_stub_length * outward[0]),
            _rounded(anchor[1] + settings.local_label_stub_length * outward[1]),
        )
        inward = (-outward[0], -outward[1])
        target_orientation = {
            (1, 0): "R0",
            (0, 1): "R90",
            (-1, 0): "R180",
            (0, -1): "R270",
        }[inward]
        port = dict(label)
        port.update(
            {
                "occurrenceId": f"BULK_{reference}",
                "sourceSymbolId": "bulk-label",
                "targetOrient": target_orientation,
                "xy": endpoint,
            }
        )
        ports.append(port)
        bulk_stubs.append(
            {"netName": str(label["netName"]), "points": [anchor, endpoint]}
        )

    bulk_shorts = [
        {
            "netName": str(item["netName"]),
            "points": [
                anchors[str(item["reference"])][str(item["bulkPinName"])],
                anchors[str(item["reference"])][str(item["sourcePinName"])],
            ],
        }
        for item in geometry.get("localBulkShorts", [])
    ]
    annotation_stubs = [
        {
            "netName": str(item["netName"]),
            "points": [
                expression_point(start_expression, solved),
                expression_point(end_expression, solved),
            ],
        }
        for item, start_expression, end_expression in annotation_rows
    ]
    routes = [
        {
            "id": str(route["id"]),
            "netName": str(route["netName"]),
            "points": [expression_point(expression, solved) for expression in expressions],
        }
        for route, expressions in route_rows
    ]
    contacts = [
        {
            "id": str(contact["id"]),
            "netName": str(contact["netName"]),
            "points": [expression_point(expression, solved) for expression in expressions],
        }
        for contact, expressions in contact_rows
    ]
    expanded_links: list[dict[str, Any]] = []
    for rows in source_endpoint_targets.values():
        by_net: dict[str, list[Point]] = {}
        for row in rows:
            by_net.setdefault(row["netName"], []).append(
                expression_point(row["expression"], solved)
            )
        for net_name, points in by_net.items():
            if len(points) > 1:
                expanded_links.append({"netName": net_name, "points": points})

    return {
        "placements": placements,
        "orientations": orientations,
        "anchors": anchors,
        "ports": ports,
        "routes": routes,
        "contacts": contacts,
        "expandedLinks": expanded_links,
        "bulkStubs": bulk_stubs,
        "bulkShorts": bulk_shorts,
        "annotationStubs": annotation_stubs,
        "junctions": [
            {
                "netName": str(item["netName"]),
                "xy": junction_points[str(item["id"])],
                "role": item.get("role"),
            }
            for item in geometry.get("junctions", [])
        ],
        "geometryAudit": {
            "method": "exact orthogonal constraints plus direct source-diagonal segments",
            "diagonalPolicy": "preserve source direction after target-pin-offset conversion",
            "sourceEdges": len(geometry_edges),
            "horizontalEdges": sum(1 for item in geometry_edges if item["sameY"]),
            "verticalEdges": sum(1 for item in geometry_edges if item["sameX"]),
            "diagonalEdges": sum(
                1 for item in geometry_edges if not item["sameX"] and not item["sameY"]
            ),
            "xEquations": len(equations[0]),
            "yEquations": len(equations[1]),
            "xComponents": x_components,
            "yComponents": y_components,
            "afterViolationCount": 0,
            "afterViolations": [],
        },
    }

"""Portable manifest-to-Virtuoso schematic workflow.

The workflow has two explicit inputs:

* a process-independent schematic manifest with source geometry; and
* a process map describing target masters, terminal names, pin offsets, and
  CDF parameter mappings.

No device or placement intent is inferred.  The process map is audited against
the live symbol masters before import, geometry is solved deterministically by
``exact_geometry``, and the resulting cellview can be read back and verified.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Iterable, Mapping, Sequence, Union

from virtuoso_bridge.virtuoso.editor import ensure_operation_response
from virtuoso_bridge.virtuoso.ops import (
    escape_skill_string,
    open_cell_view,
    save_current_cellview,
    skill_point_list,
)
from virtuoso_bridge.virtuoso.schematic.exact_geometry import (
    ExactGeometryConfig,
    pin_orientation,
    solve_exact_geometry,
)
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_check,
    schematic_create_inst_by_master_name,
    schematic_create_pin,
)


JsonSource = Union[str, Path, Mapping[str, Any]]
PointLike = tuple[float, float]


def _load_mapping(value: JsonSource, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    path = Path(value)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object")
    return data


def load_schematic_manifest(value: JsonSource) -> dict[str, Any]:
    """Load and minimally validate a process-independent schematic manifest."""

    manifest = _load_mapping(value, label="schematic manifest")
    circuits = manifest.get("circuits")
    if not isinstance(circuits, list) or not circuits:
        raise ValueError("schematic manifest must contain a non-empty circuits list")
    seen: set[str] = set()
    for circuit in circuits:
        if not isinstance(circuit, dict):
            raise ValueError("every circuit must be a JSON object")
        name = str(circuit.get("cellName", "")).strip()
        if not name:
            raise ValueError("every circuit needs a non-empty cellName")
        if name in seen:
            raise ValueError(f"duplicate circuit cellName {name!r}")
        seen.add(name)
        for field in ("instances", "ports", "nets", "sourceGeometry"):
            if field not in circuit:
                raise ValueError(f"circuit {name!r} is missing {field}")
    return manifest


def load_process_map(value: JsonSource) -> dict[str, Any]:
    """Load and validate a target-PDK process map."""

    process_map = _load_mapping(value, label="process map")
    if not process_map.get("processes"):
        raise ValueError("process map must define at least one process")
    grid = float(process_map.get("gridUnit", 0))
    if not math.isfinite(grid) or grid <= 0:
        raise ValueError("process map gridUnit must be positive")
    for name, process in process_map["processes"].items():
        if not isinstance(process, dict) or not process.get("outputLibrary"):
            raise ValueError(f"process {name!r} needs outputLibrary")
        devices = _process_devices(process_map, str(name))
        if not devices:
            raise ValueError(f"process {name!r} has no devices")
        for key, device in devices.items():
            for field in ("library", "cell", "pinOffsets"):
                if field not in device:
                    raise ValueError(
                        f"process {name!r} device {key!r} is missing {field}"
                    )
    return process_map


def _looks_like_device(value: Any) -> bool:
    return isinstance(value, Mapping) and "library" in value and "cell" in value


def _process_devices(
    process_map: Mapping[str, Any], process: str
) -> dict[str, dict[str, Any]]:
    processes = process_map.get("processes", {})
    if process not in processes:
        raise ValueError(f"unknown process {process!r}; choose from {sorted(processes)}")
    result = copy.deepcopy(dict(process_map.get("sharedDevices") or {}))
    process_config = processes[process]
    nested = process_config.get("devices")
    if isinstance(nested, Mapping):
        result.update(copy.deepcopy(dict(nested)))
    else:
        result.update(
            {
                str(key): copy.deepcopy(value)
                for key, value in process_config.items()
                if _looks_like_device(value)
            }
        )
    return result


def _device_key(item: Mapping[str, Any]) -> str:
    return str(item["kind"] if item.get("deviceClass") == "mos" else item["deviceClass"])


def _master_key(device: Mapping[str, Any]) -> str:
    return f'{device["library"]}/{device["cell"]}'


def prepare_schematic_for_process(
    source: Mapping[str, Any],
    process_map: Mapping[str, Any],
    process: str,
) -> dict[str, Any]:
    """Apply one explicit target-PDK device map to a source circuit."""

    devices = _process_devices(process_map, process)
    prepared = copy.deepcopy(dict(source))
    for item in prepared["instances"]:
        key = _device_key(item)
        if key not in devices:
            raise ValueError(
                f'{prepared["cellName"]}/{item["reference"]}: '
                f"no process-map device {key!r}"
            )
        device = devices[key]
        item["targetLibrary"] = device["library"]
        item["targetCell"] = device["cell"]
        item["targetView"] = device.get("view", "symbol")
        pin_map = device.get("pinMap") or {}
        for node in item["nodes"]:
            pin_name = str(node["pinName"])
            node["pinName"] = pin_map.get(pin_name, pin_name)
    return prepared


def _pin_offsets(devices: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Sequence[float]]]:
    return {
        _master_key(device): {
            str(name): tuple(offset)
            for name, offset in device["pinOffsets"].items()
        }
        for device in devices.values()
    }


def plan_manifest_circuit(
    source: Mapping[str, Any],
    process_map: Mapping[str, Any],
    process: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prepare and solve one manifest circuit without contacting Virtuoso."""

    prepared = prepare_schematic_for_process(source, process_map, process)
    devices = _process_devices(process_map, process)
    geometry = ExactGeometryConfig(
        source_scale=float(process_map.get("sourceScaleInGridUnits", 1.0)),
        origin=tuple(process_map.get("originInGridUnits", (0.0, 0.0))),
        local_label_stub_length=float(
            process_map.get("localLabelStubInGridUnits", 4.0)
        ),
    )
    layout = solve_exact_geometry(prepared, _pin_offsets(devices), geometry)
    return prepared, layout


def _decode_skill_output(raw: str | None) -> str:
    text = (raw or "").strip().strip('"')
    return text.replace("\\n", "\n").replace('\\"', '"')


def _result_errors(result: Any) -> list[str]:
    errors = getattr(result, "errors", None)
    return list(errors or [])


def _read_master_offsets(
    client: Any,
    device: Mapping[str, Any],
    grid: float,
) -> dict[str, list[float]]:
    lib = escape_skill_string(str(device["library"]))
    cell = escape_skill_string(str(device["cell"]))
    view = escape_skill_string(str(device.get("view", "symbol")))
    skill = f'''
let((cv result pin fig box ctr)
  cv = dbOpenCellViewByType("{lib}" "{cell}" "{view}" "schematicSymbol" "r")
  unless(cv error("master cellview not found"))
  result = ""
  foreach(term cv~>terminals
    pin = car(term~>pins)
    fig = when(pin car(pin~>figs))
    unless(fig error("terminal pin figure not found"))
    box = fig~>bBox
    ctr = list((xCoord(car(box)) + xCoord(cadr(box))) / 2.0
               (yCoord(car(box)) + yCoord(cadr(box))) / 2.0)
    result = strcat(result sprintf(nil "%s|%.8g|%.8g\\n"
      term~>name xCoord(ctr) yCoord(ctr))))
  dbClose(cv)
  result)
'''
    response = client.execute_skill(skill, timeout=30)
    errors = _result_errors(response)
    if errors:
        raise RuntimeError(f'master probe failed for {_master_key(device)}: {errors[0]}')
    actual: dict[str, list[float]] = {}
    for row in _decode_skill_output(getattr(response, "output", "")).splitlines():
        if not row.strip():
            continue
        fields = row.split("|")
        if len(fields) != 3:
            raise ValueError(f"unexpected master probe row: {row!r}")
        name, x_text, y_text = fields
        actual[name] = [
            round(float(x_text) / grid, 9),
            round(float(y_text) / grid, 9),
        ]
    return actual


def validate_process_master_offsets(
    client: Any,
    process_map: Mapping[str, Any],
    processes: Iterable[str],
) -> list[dict[str, Any]]:
    """Fail when live symbol pin centers differ from the process map."""

    grid = float(process_map["gridUnit"])
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for process in processes:
        for device in _process_devices(process_map, process).values():
            key = _master_key(device)
            if key in seen:
                continue
            seen.add(key)
            actual = _read_master_offsets(client, device, grid)
            expected = {
                str(name): [
                    round(float(value[0]), 9),
                    round(float(value[1]), 9),
                ]
                for name, value in device["pinOffsets"].items()
            }
            if actual != expected:
                raise ValueError(
                    f"{key} terminal coordinates changed: "
                    f"expected={expected}, actual={actual}"
                )
            checked.append({"master": key, "pinOffsets": actual})
    return checked


def _q(value: str) -> str:
    return f'"{escape_skill_string(value)}"'


def _find_net_expr(net_name: str) -> str:
    return f'car(setof(x cv~>nets x~>name == {_q(net_name)}))'


def _create_net(net_name: str) -> str:
    return f'dbCreateNet(cv {_q(net_name)})'


def _bind_instance_term(instance: str, term: str, net: str) -> str:
    return (
        "let((rbInst rbTerm rbNet rbExisting) "
        f'rbInst = car(setof(x cv~>instances x~>name == {_q(instance)})) '
        'unless(rbInst error("instance not found")) '
        f'rbTerm = car(setof(x rbInst~>master~>terminals x~>name == {_q(term)})) '
        'unless(rbTerm error("terminal not found")) '
        f"rbNet = {_find_net_expr(net)} "
        'unless(rbNet error("net not found")) '
        "rbExisting = car(setof(x rbInst~>instTerms x~>term == rbTerm)) "
        "unless(rbExisting dbCreateInstTerm(rbNet rbInst rbTerm)))"
    )


def _label_instance_term_at_center(instance: str, term: str, net: str) -> str:
    return (
        "let((vbInst vbTerm vbPin vbFig vbBBox vbCenter) "
        f'vbInst = car(setof(x cv~>instances x~>name == {_q(instance)})) '
        'unless(vbInst error("label instance not found")) '
        f'vbTerm = car(setof(x vbInst~>master~>terminals x~>name == {_q(term)})) '
        'unless(vbTerm error("label terminal not found")) '
        "vbPin = car(vbTerm~>pins) "
        "vbFig = car(vbPin~>figs) "
        "vbBBox = dbTransformBBox(vbFig~>bBox vbInst~>transform) "
        "vbCenter = list((xCoord(car(vbBBox)) + xCoord(cadr(vbBBox))) / 2.0 "
        "                (yCoord(car(vbBBox)) + yCoord(cadr(vbBBox))) / 2.0) "
        f'schCreateWireLabel(cv nil vbCenter {_q(net)} '
        '"lowerCenter" "R0" "stick" 0.05 nil))'
    )


def _wire_on_net(points: Sequence[tuple[float, float]], net_name: str) -> str | None:
    compact = [points[0]] if points else []
    for point in points[1:]:
        if point != compact[-1]:
            compact.append(point)
    if len(compact) < 2:
        return None
    return (
        "let((rbNet rbWires) "
        f"rbNet = {_find_net_expr(net_name)} "
        'unless(rbNet error("wire net not found")) '
        f'rbWires = schCreateWire(cv "route" "full" {skill_point_list(compact)} 0 0 0 nil nil) '
        "foreach(rbFig rbWires dbAddFigToNet(rbFig rbNet)) "
        "rbWires)"
    )


def _parameter_value(name: str, value: Any) -> str:
    text = str(value)
    if name in {"l", "w"} and re.fullmatch(r"[0-9]*\.?[0-9]+", text):
        return f"{text}u"
    return text


def _mapped_parameters(
    item: Mapping[str, Any], device: Mapping[str, Any]
) -> dict[str, str]:
    source_params = dict(item.get("sourceParameters") or {})
    if item.get("deviceClass") == "mos":
        source_params.setdefault("l", "0.15u")
        source_params.setdefault("w", "1u")
        source_params.setdefault("nf", "1")
        source_params.setdefault("m", "1")
    source_params.update(device.get("parameterOverrides", {}))
    return {
        str(target): _parameter_value(str(source_name), source_params[source_name])
        for source_name, target in device.get("parameterMap", {}).items()
        if source_name in source_params and source_params[source_name] not in (None, "")
    }


def _set_instance_properties(
    item: Mapping[str, Any], device: Mapping[str, Any]
) -> str | None:
    mapped = _mapped_parameters(item, device)
    metadata = {
        "vbSourceId": str(item.get("id") or ""),
        "vbSourceTarget": str(item.get("sourceTarget") or ""),
    }
    body = [
        "let((rbInst rbICDF rbCCDF rbSaved cdfgData cdfgForm rbParam rbCallback)",
        f'rbInst = car(setof(x cv~>instances x~>name == {_q(str(item["reference"]))}))',
        'unless(rbInst error("instance not found for parameters"))',
    ]
    if mapped:
        body.extend(
            (
                "rbICDF = cdfGetInstCDF(rbInst)",
                'unless(rbICDF error("instance has no CDF"))',
                "rbCCDF = cdfGetCellCDF(ddGetObj(rbInst~>libName rbInst~>cellName))",
                'unless(rbCCDF error("cell CDF not found"))',
                "rbSaved = makeTable('vbSaved)",
                "foreach(rbParam rbCCDF~>parameters setarray(rbSaved rbParam~>name rbParam~>value))",
                "foreach(rbParam rbCCDF~>parameters when(get(rbICDF rbParam~>name) putpropq(rbParam get(rbICDF rbParam~>name)~>value value)))",
                "cdfgData = rbCCDF",
                "cdfgForm = rbCCDF",
            )
        )
        for name, value in mapped.items():
            body.extend(
                (
                    f"rbParam = get(rbCCDF {_q(name)})",
                    f'unless(rbParam error("unknown CDF parameter: {escape_skill_string(name)}"))',
                    f"rbParam~>value = {_q(value)}",
                )
            )
        for name in mapped:
            body.extend(
                (
                    f"rbParam = get(rbCCDF {_q(name)})",
                    "rbCallback = rbParam~>callback",
                    'when(rbCallback && rbCallback != "" errset(evalstring(rbCallback) t))',
                )
            )
        body.extend(
            (
                "cdfUpdateInstParam(rbInst)",
                "foreach(rbParam rbCCDF~>parameters putpropq(rbParam arrayref(rbSaved rbParam~>name) value))",
            )
        )
    for name, value in metadata.items():
        body.append(f'dbReplaceProp(rbInst {_q(name)} "string" {_q(value)})')
    body.append("rbInst)")
    return " ".join(body)


def _point_on_open_axis_segment(
    point: PointLike,
    start: PointLike,
    end: PointLike,
) -> bool:
    if start[0] == end[0] == point[0]:
        return min(start[1], end[1]) < point[1] < max(start[1], end[1])
    if start[1] == end[1] == point[1]:
        return min(start[0], end[0]) < point[0] < max(start[0], end[0])
    return False


def apply_terminal_escape_detours(
    source: Mapping[str, Any],
    layout: dict[str, Any],
    *,
    clearance: float = 6.0,
) -> list[dict[str, Any]]:
    """Dogleg a terminal edge only when it crosses another-net target pin."""

    if clearance <= 0:
        raise ValueError("clearance must be positive")
    origins = {
        str(item["reference"]): tuple(layout["placements"][str(item["reference"])])
        for item in source["instances"]
    }
    anchors: list[dict[str, Any]] = []
    for item in source["instances"]:
        reference = str(item["reference"])
        for node in item["nodes"]:
            pin_name = str(node["pinName"])
            anchors.append(
                {
                    "reference": reference,
                    "pinName": pin_name,
                    "netName": str(node["netName"]),
                    "point": tuple(layout["anchors"][reference][pin_name]),
                }
            )

    point_use: dict[tuple[str, PointLike], int] = {}
    for route in layout["routes"]:
        for point in route["points"]:
            key = (str(route["netName"]), tuple(point))
            point_use[key] = point_use.get(key, 0) + 1

    adjustments: list[dict[str, Any]] = []
    for route in layout["routes"]:
        points = [tuple(point) for point in route["points"]]
        if not points:
            continue
        rewritten: list[PointLike] = [points[0]]
        for index, (start, end) in enumerate(zip(points, points[1:])):
            obstacles = [
                anchor
                for anchor in anchors
                if anchor["netName"] != route["netName"]
                and _point_on_open_axis_segment(anchor["point"], start, end)
            ]
            if not obstacles:
                rewritten.append(end)
                continue
            endpoint_candidates = [
                anchor
                for anchor in anchors
                if anchor["netName"] == route["netName"]
                and anchor["point"] in (start, end)
            ]
            if not endpoint_candidates:
                raise ValueError(
                    f'{source["cellName"]}/{route["id"]}:{index} crosses '
                    f"foreign pins without a terminal endpoint: {obstacles}"
                )
            anchor = sorted(
                endpoint_candidates,
                key=lambda item: (item["reference"], item["pinName"]),
            )[0]
            origin = origins[anchor["reference"]]
            if start[0] == end[0]:
                direction = -1 if anchor["point"][0] < origin[0] else 1
                offset = direction * clearance
                shifted_start = (start[0] + offset, start[1])
                if anchor["point"] == end and point_use[(route["netName"], start)] == 1:
                    rewritten[-1] = shifted_start
                dogleg = [shifted_start, (end[0] + offset, end[1]), end]
            elif start[1] == end[1]:
                direction = -1 if anchor["point"][1] < origin[1] else 1
                offset = direction * clearance
                shifted_start = (start[0], start[1] + offset)
                if anchor["point"] == end and point_use[(route["netName"], start)] == 1:
                    rewritten[-1] = shifted_start
                dogleg = [shifted_start, (end[0], end[1] + offset), end]
            else:
                raise ValueError(
                    f'{source["cellName"]}/{route["id"]}:{index} has a '
                    "diagonal foreign-pin crossing"
                )
            for point in dogleg:
                if point != rewritten[-1]:
                    rewritten.append(point)
            adjustments.append(
                {
                    "routeId": route["id"],
                    "edgeIndex": index,
                    "terminal": f'{anchor["reference"]}.{anchor["pinName"]}',
                    "foreignPins": [
                        f'{item["reference"]}.{item["pinName"]}' for item in obstacles
                    ],
                    "clearance": clearance,
                }
            )
        route["points"] = rewritten
    return adjustments


def _point_to_uu(point: Sequence[float], grid: float) -> PointLike:
    return round(float(point[0]) * grid, 9), round(float(point[1]) * grid, 9)


def _orthogonal_segments(start: PointLike, end: PointLike) -> list[list[PointLike]]:
    if start == end:
        return []
    if start[0] == end[0] or start[1] == end[1]:
        return [[start, end]]
    bend = (end[0], start[1])
    return [[start, bend], [bend, end]]


def _add_path_commands(
    commands: list[str],
    rows: Sequence[Mapping[str, Any]],
    grid: float,
    *,
    orthogonal: bool,
) -> int:
    count = 0
    for row in rows:
        points = [_point_to_uu(point, grid) for point in row["points"]]
        if orthogonal:
            segments: list[list[PointLike]] = []
            for point in points[1:]:
                segments.extend(_orthogonal_segments(points[0], point))
        else:
            # Some IC6.1.8 builds keep only the first leg of a multi-bend wire.
            segments = [list(pair) for pair in zip(points, points[1:])]
        for segment in segments:
            command = _wire_on_net(segment, str(row["netName"]))
            if command:
                commands.append(command)
                count += 1
    return count


def _delete_schematic(client: Any, library: str, cell: str) -> None:
    response = client.execute_skill(
        "let((obj) "
        f'obj = ddGetObj({_q(library)} {_q(cell)} "schematic") '
        "when(obj ddDeleteObj(obj)))",
        timeout=30,
    )
    errors = _result_errors(response)
    if errors:
        raise RuntimeError(f"cannot replace {library}/{cell}: {errors[0]}")


def import_manifest_circuit(
    client: Any,
    source: Mapping[str, Any],
    process_map: Mapping[str, Any],
    process: str,
    *,
    timeout: int = 180,
    terminal_clearance_in_grid_units: float = 6.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Import one circuit and return ``(prepared_source, result)``."""

    prepared, layout = plan_manifest_circuit(source, process_map, process)
    devices = _process_devices(process_map, process)
    process_config = process_map["processes"][process]
    library = str(process_config["outputLibrary"])
    cell = str(prepared["cellName"])
    grid = float(process_map["gridUnit"])
    routing_adjustments = apply_terminal_escape_detours(
        prepared,
        layout,
        clearance=terminal_clearance_in_grid_units,
    )
    _delete_schematic(client, library, cell)

    commands = [open_cell_view(library, cell, view="schematic", mode="w")]
    commands.extend(_create_net(str(name)) for name in prepared["nets"])
    for item in prepared["instances"]:
        reference = str(item["reference"])
        xy = _point_to_uu(layout["placements"][reference], grid)
        commands.append(
            schematic_create_inst_by_master_name(
                str(item["targetLibrary"]),
                str(item["targetCell"]),
                str(item.get("targetView", "symbol")),
                reference,
                xy[0],
                xy[1],
                layout["orientations"][reference],
            )
        )
        property_command = _set_instance_properties(item, devices[_device_key(item)])
        if property_command:
            commands.append(property_command)
        for node in item["nodes"]:
            commands.append(
                _bind_instance_term(
                    reference,
                    str(node["pinName"]),
                    str(node["netName"]),
                )
            )

    wire_commands = _add_path_commands(
        commands, layout["routes"], grid, orthogonal=False
    )
    for key in (
        "contacts",
        "expandedLinks",
        "bulkStubs",
        "bulkShorts",
        "annotationStubs",
    ):
        wire_commands += _add_path_commands(
            commands, layout[key], grid, orthogonal=True
        )
    for port in layout["ports"]:
        xy = _point_to_uu(port["xy"], grid)
        commands.append(
            schematic_create_pin(
                str(port["name"]),
                xy[0],
                xy[1],
                pin_orientation(port),
                direction="inputOutput",
            )
        )

    expanded_supply_labels: list[str] = []
    for item in prepared["instances"]:
        if item.get("sourceInvocationKind") != "expanded-subcircuit":
            continue
        source_node = next(
            (node for node in item["nodes"] if node["pinName"] == "S"),
            None,
        )
        if source_node is None:
            continue
        commands.append(
            _label_instance_term_at_center(
                str(item["reference"]),
                "S",
                str(source_node["netName"]),
            )
        )
        expanded_supply_labels.append(
            f'{item["reference"]}.S={source_node["netName"]}'
        )
    commands.extend((schematic_check(), save_current_cellview()))

    started = time.monotonic()
    response = client.execute_operations(commands, timeout=timeout)
    ensure_operation_response(response, context=f"import {library}/{cell}")
    elapsed = time.monotonic() - started
    warnings = list(getattr(response, "warnings", None) or [])
    if isinstance(response, dict):
        warnings = list(response.get("warnings") or [])
    result = {
        "process": process,
        "library": library,
        "cellName": cell,
        "instances": len(prepared["instances"]),
        "logicalPorts": [str(item["name"]) for item in prepared["ports"]],
        "portOccurrences": len(layout["ports"]),
        "nets": len(prepared["nets"]),
        "wireCommands": wire_commands,
        "operationCount": len(commands),
        "elapsedSeconds": round(elapsed, 3),
        "geometryAudit": layout["geometryAudit"],
        "routingAdjustments": routing_adjustments,
        "expandedSupplyLabels": expanded_supply_labels,
        "warnings": warnings,
    }
    for key in ("galleryId", "galleryName", "sourceId", "sourceName"):
        if key in prepared:
            result[key] = prepared[key]
    return prepared, result


def _engineering_number(value: Any) -> float | None:
    match = re.fullmatch(
        r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*"
        r"([a-zA-Z]+)?\s*",
        str(value),
    )
    if not match:
        return None
    scale = {
        "": 1.0,
        "f": 1e-15,
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "m": 1e-3,
        "k": 1e3,
        "meg": 1e6,
        "g": 1e9,
        "t": 1e12,
    }.get((match.group(2) or "").lower())
    if scale is None:
        return None
    return float(match.group(1)) * scale


def _parameter_values_match(expected: Any, actual: Any) -> bool:
    expected_number = _engineering_number(expected)
    actual_number = _engineering_number(actual)
    if expected_number is not None and actual_number is not None:
        return math.isclose(expected_number, actual_number, rel_tol=1e-9, abs_tol=1e-24)
    return str(expected) == str(actual)


def verify_manifest_circuit(
    client: Any,
    prepared_source: Mapping[str, Any],
    result: Mapping[str, Any],
    process_map: Mapping[str, Any],
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    """Read a generated cell back and verify masters, nets, pins, and CDFs."""

    from virtuoso_bridge.virtuoso.schematic.reader import read_schematic

    devices = _process_devices(process_map, str(result["process"]))
    data = read_schematic(
        client,
        str(result["library"]),
        str(result["cellName"]),
        include_positions=True,
        param_filters=None,
        timeout=timeout,
    )
    by_name = {item["name"]: item for item in data["instances"]}
    errors: list[str] = []
    actual_by_expected_net: dict[str, set[str]] = {}
    expected_terms_by_net: dict[str, list[str]] = {}
    component_terms: dict[str, dict[str, list[str]]] = {}
    parameter_errors: list[str] = []
    if len(by_name) != len(prepared_source["instances"]):
        errors.append(
            f'instance count {len(by_name)} != {len(prepared_source["instances"])}'
        )
    for expected in prepared_source["instances"]:
        reference = str(expected["reference"])
        actual = by_name.get(reference)
        if actual is None:
            errors.append(f"missing instance {reference}")
            continue
        expected_master = (expected["targetLibrary"], expected["targetCell"])
        if (actual["lib"], actual["cell"]) != expected_master:
            errors.append(
                f'{reference} master {actual["lib"]}/{actual["cell"]} != '
                f'{expected_master[0]}/{expected_master[1]}'
            )
        for name, expected_value in _mapped_parameters(
            expected, devices[_device_key(expected)]
        ).items():
            actual_value = actual.get("params", {}).get(name)
            if actual_value is None:
                parameter_errors.append(f"{reference}.{name} is missing")
            elif not _parameter_values_match(expected_value, actual_value):
                parameter_errors.append(
                    f"{reference}.{name}={actual_value!r} != {expected_value!r}"
                )
        for node in expected["nodes"]:
            term = str(node["pinName"])
            expected_net = str(node["netName"])
            expected_terms_by_net.setdefault(expected_net, []).append(
                f"{reference}.{term}"
            )
            actual_net = actual.get("terms", {}).get(term)
            if actual_net is None:
                errors.append(f"{reference}.{term} is unconnected")
            else:
                actual_by_expected_net.setdefault(expected_net, set()).add(actual_net)
                component_terms.setdefault(expected_net, {}).setdefault(
                    actual_net, []
                ).append(f"{reference}.{term}")

    for source_net, actual_nets in sorted(actual_by_expected_net.items()):
        if len(actual_nets) != 1:
            errors.append(
                f"source net {source_net!r} split into {sorted(actual_nets)} at "
                f"{expected_terms_by_net[source_net]}"
            )
    by_actual: dict[str, list[str]] = {}
    for source_net, actual_nets in actual_by_expected_net.items():
        for actual_net in actual_nets:
            by_actual.setdefault(actual_net, []).append(source_net)
    merged_components: dict[str, list[str]] = {}
    for actual_net, source_nets in sorted(by_actual.items()):
        if len(source_nets) > 1:
            merged_components[actual_net] = sorted(source_nets)
            errors.append(f"source nets {sorted(source_nets)} merged as {actual_net!r}")

    expected_pins = {str(item["name"]) for item in prepared_source["ports"]}
    missing_pins = sorted(expected_pins - set(data["pins"]))
    if missing_pins:
        errors.append(f"missing pins {missing_pins}")
    bad_directions = sorted(
        name
        for name, pin in data["pins"].items()
        if name in expected_pins and pin["direction"] != "inputOutput"
    )
    if bad_directions:
        errors.append(f"non-inout pins {bad_directions}")
    errors.extend(parameter_errors)
    return {
        "passed": not errors,
        "errors": errors,
        "splitComponents": {
            net: components
            for net, components in component_terms.items()
            if len(components) > 1
        },
        "mergedComponents": merged_components,
        "parameterErrors": parameter_errors,
        "readbackInstances": len(by_name),
        "readbackNets": len(data["nets"]),
        "readbackPins": len(data["pins"]),
    }


def reconcile_split_net_components(
    client: Any,
    library: str,
    cell: str,
    verification: Mapping[str, Any],
    *,
    timeout: int = 120,
) -> list[str]:
    """Restore one logical net across intentionally disconnected drawing islands."""

    if verification.get("mergedComponents"):
        return []
    commands = [open_cell_view(library, cell, view="schematic", mode="a")]
    merges: list[str] = []
    for source_net, components in sorted(
        verification.get("splitComponents", {}).items()
    ):
        actual_nets = sorted(components)
        survivor = source_net if source_net in components else actual_nets[0]
        for actual_net in actual_nets:
            if actual_net == survivor:
                continue
            commands.append(
                "let((vbKeep vbOther) "
                f"vbKeep = {_find_net_expr(survivor)} "
                f"vbOther = {_find_net_expr(actual_net)} "
                'unless(vbKeep error("surviving split net not found")) '
                'unless(vbOther error("split net component not found")) '
                "dbMergeNet(vbKeep vbOther))"
            )
            merges.append(f"{actual_net}->{source_net}")
        if survivor != source_net:
            commands.append(
                "let((vbNet) "
                f"vbNet = {_find_net_expr(survivor)} "
                'unless(vbNet error("split net survivor not found")) '
                f"dbRenameNet(vbNet {_q(source_net)}))"
            )
            merges.append(f"{survivor}->{source_net}")
    if not merges:
        return []
    # A second schCheck would split intentionally disconnected drawing islands
    # again, so this reconciliation step only saves the explicit OA net merge.
    commands.append(save_current_cellview())
    response = client.execute_operations(commands, timeout=timeout)
    ensure_operation_response(
        response,
        context=f"reconcile split nets in {library}/{cell}",
    )
    return merges


def import_schematic_manifest(
    client: Any,
    manifest: JsonSource,
    process_map: JsonSource,
    *,
    processes: Sequence[str] | None = None,
    cells: Sequence[str] | None = None,
    verify: bool = True,
    validate_masters: bool = True,
    timeout: int = 180,
) -> dict[str, Any]:
    """Import selected circuits into one or more mapped PDK libraries."""

    source_data = load_schematic_manifest(manifest)
    map_data = load_process_map(process_map)
    selected_processes = list(processes or map_data["processes"].keys())
    for name in selected_processes:
        _process_devices(map_data, name)
    requested_cells = set(cells or ())
    selected_circuits = [
        item
        for item in source_data["circuits"]
        if not requested_cells or item["cellName"] in requested_cells
    ]
    missing = sorted(requested_cells - {item["cellName"] for item in selected_circuits})
    if missing:
        raise ValueError(f"unknown cells: {missing}")
    if not selected_circuits:
        raise ValueError("no circuits selected")

    master_checks = (
        validate_process_master_offsets(client, map_data, selected_processes)
        if validate_masters
        else []
    )
    imported: list[dict[str, Any]] = []
    for process in selected_processes:
        for source in selected_circuits:
            prepared, result = import_manifest_circuit(
                client,
                source,
                map_data,
                process,
                timeout=timeout,
            )
            if verify:
                result["verification"] = verify_manifest_circuit(
                    client,
                    prepared,
                    result,
                    map_data,
                    timeout=min(timeout, 120),
                )
                if result["verification"]["splitComponents"]:
                    result["reconciledNetMerges"] = reconcile_split_net_components(
                        client,
                        str(result["library"]),
                        str(result["cellName"]),
                        result["verification"],
                        timeout=min(timeout, 120),
                    )
                    if result["reconciledNetMerges"]:
                        result["verification"] = verify_manifest_circuit(
                            client,
                            prepared,
                            result,
                            map_data,
                            timeout=min(timeout, 120),
                        )
                if not result["verification"]["passed"]:
                    raise RuntimeError(
                        f'{result["library"]}/{result["cellName"]} verification failed: '
                        + "; ".join(result["verification"]["errors"])
                    )
            imported.append(result)
    return {
        "schema": "virtuoso-bridge-schematic-import-result-v1",
        "sourceSchema": source_data.get("schema"),
        "processes": selected_processes,
        "masterChecks": master_checks,
        "circuits": imported,
        "passed": all(
            row.get("verification", {}).get("passed", True) for row in imported
        ),
    }


def capture_schematic_cell(
    client: Any,
    library: str,
    cell: str,
    output: str | Path,
    *,
    view: str = "schematic",
    margin: float = 0.75,
    timeout: int = 180,
) -> Path:
    """Open, fit, and capture one exact Virtuoso editor window."""

    if not math.isfinite(margin) or margin <= 0:
        raise ValueError("margin must be positive")
    opened = client.open_window(library, cell, view=view, timeout=timeout)
    errors = _result_errors(opened)
    if errors:
        raise RuntimeError(f"open {library}/{cell}: {errors[0]}")
    match = re.search(r"window:(\d+)", str(getattr(opened, "output", "")))
    if match is None:
        raise RuntimeError(f"cannot resolve editor window: {getattr(opened, 'output', None)!r}")
    window_number = int(match.group(1))
    zoomed = client.execute_skill(
        "let((vbWindow) "
        "foreach(w hiGetWindowList() "
        f"  when(w~>windowNum == {window_number} vbWindow = w)) "
        'unless(vbWindow error("editor window not found")) '
        "hiZoomIn(vbWindow vbWindow~>cellView~>bBox) "
        f"hiZoomRelativeScale(vbWindow {margin:g}))",
        timeout=30,
    )
    errors = _result_errors(zoomed)
    if errors:
        raise RuntimeError(f"zoom {library}/{cell}: {errors[0]}")
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    captured = client.screenshot(output=path, target=window_number, timeout=60)
    errors = _result_errors(captured)
    if errors:
        raise RuntimeError(f"screenshot {library}/{cell}: {errors[0]}")
    return path


def capture_import_result(
    client: Any,
    import_result: Mapping[str, Any],
    output_dir: str | Path,
    *,
    margin: float = 0.75,
) -> list[Path]:
    """Capture every cell listed in an import result with stable filenames."""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for row in import_result.get("circuits", []):
        process = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row["process"]))
        cell = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row["cellName"]))
        output = directory / f"{process}_{cell}_virtuoso.png"
        outputs.append(
            capture_schematic_cell(
                client,
                str(row["library"]),
                str(row["cellName"]),
                output,
                margin=margin,
            )
        )
    return outputs

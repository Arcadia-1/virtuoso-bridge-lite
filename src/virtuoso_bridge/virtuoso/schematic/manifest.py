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
from collections import Counter
from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Iterable, Mapping, Sequence, Union
import uuid

from virtuoso_bridge.virtuoso.editor import ensure_operation_response
from virtuoso_bridge.virtuoso.ops import (
    default_view_type_for,
    escape_skill_string,
    open_cell_view,
    skill_point,
    skill_point_list,
)
from virtuoso_bridge.virtuoso.schematic.exact_geometry import (
    ExactGeometryConfig,
    pin_orientation,
    solve_exact_geometry,
)
from virtuoso_bridge.virtuoso.schematic.diagnostics import check_and_save_schematic
from virtuoso_bridge.virtuoso.response import response_fields
from virtuoso_bridge.virtuoso.skill_output import (
    is_single_complete_skill_list,
    parse_sexpr,
)


JsonSource = Union[str, Path, Mapping[str, Any]]
PointLike = tuple[float, float]

_MANIFEST_SCHEMA = "virtuoso-bridge-exact-schematic-v1"
_PROCESS_MAP_SCHEMA = "virtuoso-bridge-process-map-v1"
_MIRRORS = {"none", "horizontal", "vertical", "both"}


def _load_mapping(value: JsonSource, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    path = Path(value)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object")
    return data


def _nonempty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def _validate_point(value: Any, *, field: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object with x and y")
    _finite_number(value.get("x"), field=f"{field}.x")
    _finite_number(value.get("y"), field=f"{field}.y")


def _validate_transform(value: Any, *, field: str) -> None:
    transform = value or {}
    if not isinstance(transform, Mapping):
        raise ValueError(f"{field} must be an object")
    rotation = _finite_number(transform.get("rotation", 0), field=f"{field}.rotation")
    if not rotation.is_integer() or int(rotation) % 90:
        raise ValueError(f"{field}.rotation must be a multiple of 90 degrees")
    mirror = str(transform.get("mirror", "none"))
    if mirror not in _MIRRORS:
        raise ValueError(f"{field}.mirror must be one of {sorted(_MIRRORS)}")


def _unique_rows(
    rows: Any,
    *,
    field: str,
    key: str,
) -> tuple[list[Mapping[str, Any]], set[str]]:
    if not isinstance(rows, list):
        raise ValueError(f"{field} must be a list")
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"{field}[{index}] must be an object")
        identifier = _nonempty_string(row.get(key), field=f"{field}[{index}].{key}")
        if identifier in seen:
            raise ValueError(f"duplicate {field} {key} {identifier!r}")
        seen.add(identifier)
        result.append(row)
    return result, seen


def _validate_manifest_circuit(circuit: Mapping[str, Any]) -> None:
    cell = _nonempty_string(circuit.get("cellName"), field="circuit.cellName")
    prefix = f"circuit {cell!r}"
    nets_raw = circuit.get("nets")
    if not isinstance(nets_raw, list) or not nets_raw:
        raise ValueError(f"{prefix} nets must be a non-empty list")
    nets = [_nonempty_string(value, field=f"{prefix}.nets") for value in nets_raw]
    if len(nets) != len(set(nets)):
        raise ValueError(f"{prefix} has duplicate net names")
    net_names = set(nets)

    instances, instance_ids = _unique_rows(
        circuit.get("instances"), field=f"{prefix}.instances", key="id"
    )
    references: set[str] = set()
    instance_pins: dict[str, set[str]] = {}
    source_terms: set[tuple[str, str, str]] = set()
    for index, item in enumerate(instances):
        where = f"{prefix}.instances[{index}]"
        reference = _nonempty_string(item.get("reference"), field=f"{where}.reference")
        if reference in references:
            raise ValueError(f"{prefix} has duplicate instance reference {reference!r}")
        references.add(reference)
        device_class = _nonempty_string(
            item.get("deviceClass"), field=f"{where}.deviceClass"
        )
        if device_class == "mos":
            _nonempty_string(item.get("kind"), field=f"{where}.kind")
        _validate_point(item.get("sourcePosition"), field=f"{where}.sourcePosition")
        _validate_transform(item.get("sourceTransform"), field=f"{where}.sourceTransform")
        nodes = item.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(f"{where}.nodes must be a non-empty list")
        target_pins: set[str] = set()
        source_id = str(item.get("sourceExpandedFrom") or item["id"])
        for node_index, node in enumerate(nodes):
            node_where = f"{where}.nodes[{node_index}]"
            if not isinstance(node, Mapping):
                raise ValueError(f"{node_where} must be an object")
            source_pin = _nonempty_string(
                node.get("sourcePinName"), field=f"{node_where}.sourcePinName"
            )
            target_pin = _nonempty_string(
                node.get("pinName"), field=f"{node_where}.pinName"
            )
            net_name = _nonempty_string(
                node.get("netName"), field=f"{node_where}.netName"
            )
            if net_name not in net_names:
                raise ValueError(f"{node_where} references unknown net {net_name!r}")
            if target_pin in target_pins:
                raise ValueError(f"{where} has duplicate target pin {target_pin!r}")
            target_pins.add(target_pin)
            source_terms.add((source_id, source_pin, net_name))
        instance_pins[reference] = target_pins

    ports, port_names = _unique_rows(
        circuit.get("ports"), field=f"{prefix}.ports", key="name"
    )
    logical_port_nets: dict[str, str] = {}
    for index, port in enumerate(ports):
        net_name = _nonempty_string(
            port.get("netName"), field=f"{prefix}.ports[{index}].netName"
        )
        if net_name not in net_names:
            raise ValueError(f"{prefix}.ports[{index}] references unknown net {net_name!r}")
        logical_port_nets[str(port["name"])] = net_name

    geometry = circuit.get("sourceGeometry")
    if not isinstance(geometry, Mapping):
        raise ValueError(f"{prefix}.sourceGeometry must be an object")
    bounds = geometry.get("bounds")
    if not isinstance(bounds, Mapping):
        raise ValueError(f"{prefix}.sourceGeometry.bounds must be an object")
    min_x = _finite_number(bounds.get("minX"), field=f"{prefix}.bounds.minX")
    min_y = _finite_number(bounds.get("minY"), field=f"{prefix}.bounds.minY")
    max_x = _finite_number(bounds.get("maxX"), field=f"{prefix}.bounds.maxX")
    max_y = _finite_number(bounds.get("maxY"), field=f"{prefix}.bounds.maxY")
    if min_x > max_x or min_y > max_y:
        raise ValueError(f"{prefix}.sourceGeometry.bounds is inverted")

    occurrences, occurrence_ids = _unique_rows(
        geometry.get("portOccurrences", []),
        field=f"{prefix}.portOccurrences",
        key="occurrenceId",
    )
    occurrence_nets: dict[str, str] = {}
    for index, port in enumerate(occurrences):
        where = f"{prefix}.portOccurrences[{index}]"
        name = _nonempty_string(port.get("name"), field=f"{where}.name")
        if name not in port_names:
            raise ValueError(f"{where} references unknown logical port {name!r}")
        net_name = _nonempty_string(port.get("netName"), field=f"{where}.netName")
        if net_name not in net_names:
            raise ValueError(f"{where} references unknown net {net_name!r}")
        occurrence_nets[str(port["occurrenceId"])] = net_name
        _validate_point(port.get("sourcePosition"), field=f"{where}.sourcePosition")
        _validate_transform(port.get("sourceTransform"), field=f"{where}.sourceTransform")

    junctions, junction_ids = _unique_rows(
        geometry.get("junctions", []),
        field=f"{prefix}.junctions",
        key="id",
    )
    junction_nets: dict[str, str] = {}
    for index, junction in enumerate(junctions):
        where = f"{prefix}.junctions[{index}]"
        net_name = _nonempty_string(junction.get("netName"), field=f"{where}.netName")
        if net_name not in net_names:
            raise ValueError(f"{where} references unknown net {net_name!r}")
        junction_nets[str(junction["id"])] = net_name
        _validate_point(junction.get("sourcePosition"), field=f"{where}.sourcePosition")

    def validate_endpoint(endpoint: Any, *, where: str, net_name: str) -> None:
        if not isinstance(endpoint, Mapping):
            raise ValueError(f"{where} must be an object")
        kind = endpoint.get("kind")
        _validate_point(endpoint.get("sourcePoint"), field=f"{where}.sourcePoint")
        if kind == "junction":
            junction_id = _nonempty_string(
                endpoint.get("junctionId"), field=f"{where}.junctionId"
            )
            if junction_id not in junction_ids:
                raise ValueError(f"{where} references unknown junction {junction_id!r}")
            if junction_nets[junction_id] != net_name:
                raise ValueError(f"{where} junction is on a different net")
            return
        if kind not in {"instance", "port"}:
            raise ValueError(f"{where}.kind must be instance, port, or junction")
        instance_id = _nonempty_string(
            endpoint.get("instanceId"), field=f"{where}.instanceId"
        )
        pin_name = _nonempty_string(endpoint.get("pinName"), field=f"{where}.pinName")
        if kind == "port":
            if instance_id not in occurrence_ids:
                raise ValueError(f"{where} references unknown port occurrence {instance_id!r}")
            if occurrence_nets[instance_id] != net_name:
                raise ValueError(f"{where} port occurrence is on a different net")
        elif (instance_id, pin_name, net_name) not in source_terms:
            raise ValueError(
                f"{where} cannot resolve instance endpoint "
                f"{instance_id}.{pin_name} on net {net_name!r}"
            )

    routes, _ = _unique_rows(
        geometry.get("routes", []), field=f"{prefix}.routes", key="id"
    )
    for index, route in enumerate(routes):
        where = f"{prefix}.routes[{index}]"
        net_name = _nonempty_string(route.get("netName"), field=f"{where}.netName")
        if net_name not in net_names:
            raise ValueError(f"{where} references unknown net {net_name!r}")
        validate_endpoint(route.get("start"), where=f"{where}.start", net_name=net_name)
        steps = route.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{where}.steps must be a non-empty list")
        for step_index, step in enumerate(steps):
            step_where = f"{where}.steps[{step_index}]"
            if isinstance(step, Mapping) and step.get("kind") == "bend":
                _validate_point(step.get("position"), field=f"{step_where}.position")
            else:
                validate_endpoint(step, where=step_where, net_name=net_name)

    contacts, _ = _unique_rows(
        geometry.get("contacts", []), field=f"{prefix}.contacts", key="id"
    )
    for index, contact in enumerate(contacts):
        where = f"{prefix}.contacts[{index}]"
        net_name = _nonempty_string(contact.get("netName"), field=f"{where}.netName")
        if net_name not in net_names:
            raise ValueError(f"{where} references unknown net {net_name!r}")
        endpoints = contact.get("endpoints")
        if not isinstance(endpoints, list) or len(endpoints) < 2:
            raise ValueError(f"{where}.endpoints needs at least two endpoints")
        for endpoint_index, endpoint in enumerate(endpoints):
            validate_endpoint(
                endpoint,
                where=f"{where}.endpoints[{endpoint_index}]",
                net_name=net_name,
            )

    for field in ("localBulkLabels", "localBulkShorts"):
        rows = geometry.get(field, [])
        if not isinstance(rows, list):
            raise ValueError(f"{prefix}.{field} must be a list")
        for index, row in enumerate(rows):
            where = f"{prefix}.{field}[{index}]"
            if not isinstance(row, Mapping):
                raise ValueError(f"{where} must be an object")
            reference = _nonempty_string(row.get("reference"), field=f"{where}.reference")
            if reference not in references:
                raise ValueError(f"{where} references unknown instance {reference!r}")
            net_name = _nonempty_string(row.get("netName"), field=f"{where}.netName")
            if net_name not in net_names:
                raise ValueError(f"{where} references unknown net {net_name!r}")
            pin_fields = ("pinName",) if field == "localBulkLabels" else (
                "bulkPinName",
                "sourcePinName",
            )
            for pin_field in pin_fields:
                pin_name = _nonempty_string(
                    row.get(pin_field), field=f"{where}.{pin_field}"
                )
                if pin_name not in instance_pins[reference]:
                    raise ValueError(
                        f"{where}.{pin_field} references unknown pin {pin_name!r}"
                    )
            if field == "localBulkLabels":
                name = _nonempty_string(row.get("name"), field=f"{where}.name")
                if name not in port_names or logical_port_nets[name] != net_name:
                    raise ValueError(f"{where} references an incompatible logical port")

    annotations = geometry.get("annotationStubs", [])
    if not isinstance(annotations, list):
        raise ValueError(f"{prefix}.annotationStubs must be a list")
    annotation_ids: set[str] = set()
    for index, item in enumerate(annotations):
        where = f"{prefix}.annotationStubs[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{where} must be an object")
        annotation_id = _nonempty_string(
            item.get("annotationId"), field=f"{where}.annotationId"
        )
        if annotation_id in annotation_ids:
            raise ValueError(f"duplicate annotationId {annotation_id!r}")
        annotation_ids.add(annotation_id)
        if annotation_id not in occurrence_ids:
            raise ValueError(f"{where} references unknown port occurrence {annotation_id!r}")
        net_name = _nonempty_string(item.get("netName"), field=f"{where}.netName")
        if net_name not in net_names:
            raise ValueError(f"{where} references unknown net {net_name!r}")
        if item.get("anchorJunctionId"):
            anchor = str(item["anchorJunctionId"])
            if anchor not in junction_ids or junction_nets[anchor] != net_name:
                raise ValueError(f"{where} has an invalid anchor junction")
        else:
            _validate_point(item.get("start"), field=f"{where}.start")
        _validate_point(item.get("end"), field=f"{where}.end")


def load_schematic_manifest(value: JsonSource) -> dict[str, Any]:
    """Load and fully preflight a process-independent schematic manifest."""

    manifest = _load_mapping(value, label="schematic manifest")
    if manifest.get("schema") != _MANIFEST_SCHEMA:
        raise ValueError(f"schematic manifest schema must be {_MANIFEST_SCHEMA!r}")
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
        _validate_manifest_circuit(circuit)
    return manifest


def load_process_map(value: JsonSource) -> dict[str, Any]:
    """Load and validate a target-PDK process map."""

    process_map = _load_mapping(value, label="process map")
    if process_map.get("schema") != _PROCESS_MAP_SCHEMA:
        raise ValueError(f"process map schema must be {_PROCESS_MAP_SCHEMA!r}")
    if not isinstance(process_map.get("processes"), Mapping) or not process_map["processes"]:
        raise ValueError("process map must define at least one process")
    grid = float(process_map.get("gridUnit", 0))
    if not math.isfinite(grid) or grid <= 0:
        raise ValueError("process map gridUnit must be positive")
    source_scale = _finite_number(
        process_map.get("sourceScaleInGridUnits", 1.0),
        field="process map sourceScaleInGridUnits",
    )
    if source_scale <= 0:
        raise ValueError("process map sourceScaleInGridUnits must be positive")
    origin = process_map.get("originInGridUnits", (0.0, 0.0))
    if not isinstance(origin, Sequence) or isinstance(origin, (str, bytes)) or len(origin) != 2:
        raise ValueError("process map originInGridUnits needs two numbers")
    _finite_number(origin[0], field="process map originInGridUnits[0]")
    _finite_number(origin[1], field="process map originInGridUnits[1]")
    label_length = _finite_number(
        process_map.get("localLabelStubInGridUnits", 4.0),
        field="process map localLabelStubInGridUnits",
    )
    if label_length <= 0:
        raise ValueError("process map localLabelStubInGridUnits must be positive")
    for name, process in process_map["processes"].items():
        if not isinstance(process, dict) or not process.get("outputLibrary"):
            raise ValueError(f"process {name!r} needs outputLibrary")
        _nonempty_string(process["outputLibrary"], field=f"process {name!r}.outputLibrary")
        devices = _process_devices(process_map, str(name))
        if not devices:
            raise ValueError(f"process {name!r} has no devices")
        for key, device in devices.items():
            for field in ("library", "cell", "pinOffsets"):
                if field not in device:
                    raise ValueError(
                        f"process {name!r} device {key!r} is missing {field}"
                    )
            _nonempty_string(device["library"], field=f"device {key!r}.library")
            _nonempty_string(device["cell"], field=f"device {key!r}.cell")
            offsets = device["pinOffsets"]
            if not isinstance(offsets, Mapping) or not offsets:
                raise ValueError(f"process {name!r} device {key!r} needs pinOffsets")
            for pin, offset in offsets.items():
                _nonempty_string(pin, field=f"device {key!r} pin name")
                if not isinstance(offset, Sequence) or isinstance(offset, (str, bytes)) or len(offset) != 2:
                    raise ValueError(f"device {key!r} pin offset {pin!r} needs two numbers")
                _finite_number(offset[0], field=f"device {key!r}.{pin}.x")
                _finite_number(offset[1], field=f"device {key!r}.{pin}.y")
            for map_name in ("pinMap", "parameterMap", "parameterOverrides"):
                mapping = device.get(map_name, {})
                if not isinstance(mapping, Mapping):
                    raise ValueError(f"device {key!r}.{map_name} must be an object")
            for source_pin, target_pin in device.get("pinMap", {}).items():
                _nonempty_string(source_pin, field=f"device {key!r}.pinMap source")
                mapped = _nonempty_string(
                    target_pin, field=f"device {key!r}.pinMap target"
                )
                if mapped not in offsets:
                    raise ValueError(
                        f"device {key!r} maps pin {source_pin!r} to {mapped!r} "
                        "without a pin offset"
                    )
            for source_param, target_param in device.get("parameterMap", {}).items():
                _nonempty_string(
                    source_param, field=f"device {key!r}.parameterMap source"
                )
                _nonempty_string(
                    target_param, field=f"device {key!r}.parameterMap target"
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
    pin_maps_by_reference: dict[str, Mapping[str, str]] = {}
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
        pin_maps_by_reference[str(item["reference"])] = pin_map
        for node in item["nodes"]:
            pin_name = str(node["pinName"])
            node["pinName"] = pin_map.get(pin_name, pin_name)
    geometry = prepared["sourceGeometry"]
    for label in geometry.get("localBulkLabels", []):
        pin_map = pin_maps_by_reference[str(label["reference"])]
        pin_name = str(label["pinName"])
        label["pinName"] = pin_map.get(pin_name, pin_name)
    for short in geometry.get("localBulkShorts", []):
        pin_map = pin_maps_by_reference[str(short["reference"])]
        for field in ("bulkPinName", "sourcePinName"):
            pin_name = str(short[field])
            short[field] = pin_map.get(pin_name, pin_name)
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


def _create_instance(
    item: Mapping[str, Any],
    xy: PointLike,
    orientation: str,
) -> str:
    library = str(item["targetLibrary"])
    cell = str(item["targetCell"])
    view = str(item.get("targetView", "symbol"))
    view_type = default_view_type_for(view)
    reference = str(item["reference"])
    return (
        "let((vbMaster vbInst vbResult) "
        f'vbMaster = dbOpenCellViewByType({_q(library)} {_q(cell)} {_q(view)} '
        f'{_q(view_type)} "r") '
        'unless(vbMaster error("instance master open failed")) '
        "vbResult = unwindProtect(progn("
        f'vbInst = dbCreateInst(cv vbMaster {_q(reference)} '
        f'{skill_point(xy[0], xy[1])} {_q(orientation)}) '
        'unless(vbInst error("instance creation failed")) '
        "vbInst) "
        'when(vbMaster unless(dbClose(vbMaster) error("instance master close failed")) '
        "vbMaster = nil)) vbResult)"
    )


def _create_pin(
    name: str,
    xy: PointLike,
    orientation: str,
) -> str:
    return (
        "let((vbPinMaster vbPin vbResult) "
        'vbPinMaster = dbOpenCellViewByType("basic" "iopin" "symbol" '
        '"schematicSymbol" "r") '
        'unless(vbPinMaster error("pin master open failed")) '
        "vbResult = unwindProtect(progn("
        f'vbPin = schCreatePin(cv vbPinMaster {_q(name)} "inputOutput" nil '
        f'{skill_point(xy[0], xy[1])} {_q(orientation)}) '
        f'unless(vbPin error("pin creation failed: {escape_skill_string(name)}")) '
        "vbPin) "
        'when(vbPinMaster unless(dbClose(vbPinMaster) error("pin master close failed")) '
        "vbPinMaster = nil)) vbResult)"
    )


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
        "let((rbInst rbICDF rbCCDF rbSaved cdfgData cdfgForm rbParam "
        "rbCallback rbCallbackResult rbUpdateAttempt)",
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
                "rbUpdateAttempt = errset(unwindProtect(progn(",
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
                    'when(rbCallback && rbCallback != "" '
                    "rbCallbackResult = errset(evalstring(rbCallback) nil) "
                    f'unless(rbCallbackResult error("CDF callback failed: {escape_skill_string(name)}")))',
                )
            )
        body.extend(
            (
                "cdfUpdateInstParam(rbInst)",
                "t)",
                "foreach(rbParam rbCCDF~>parameters "
                "putpropq(rbParam arrayref(rbSaved rbParam~>name) value))) nil)",
                'unless(rbUpdateAttempt error("CDF parameter update failed"))',
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


def _checked_skill_output(
    client: Any,
    skill: str,
    *,
    context: str,
    timeout: int,
) -> str:
    response = client.execute_skill(skill, timeout=timeout)
    errors, status, output = response_fields(response)
    status_value = getattr(status, "value", status)
    if errors or (
        status_value is not None
        and str(status_value).lower() not in {"success", "ok"}
    ):
        detail = errors[0] if errors else output or f"status={status_value}"
        raise RuntimeError(f"{context} failed: {detail}")
    return output.strip()


def _cellview_exists(client: Any, library: str, cell: str, *, timeout: int) -> bool:
    output = _checked_skill_output(
        client,
        "let((vbObj vbExists) "
        f'vbObj = ddGetObj({_q(library)} {_q(cell)} "schematic") '
        "vbExists = if(vbObj t nil) "
        "when(vbObj ddReleaseObj(vbObj)) "
        'list("vbExists" vbExists))',
        context=f"probe {library}/{cell}",
        timeout=timeout,
    )
    if not is_single_complete_skill_list(output):
        raise RuntimeError(f"probe {library}/{cell} returned invalid output: {output!r}")
    parsed = parse_sexpr(output)
    if not isinstance(parsed, list) or len(parsed) != 2 or parsed[0] != "vbExists":
        raise RuntimeError(f"probe {library}/{cell} returned invalid output: {output!r}")
    return parsed[1] is True


def _delete_cellviews(
    client: Any,
    library: str,
    cells: Iterable[str | None],
    *,
    timeout: int,
) -> None:
    names = [name for name in cells if name]
    if not names:
        return
    commands = []
    for name in names:
        commands.append(
            "let((vbObj) "
            f'vbObj = ddGetObj({_q(library)} {_q(name)} "schematic") '
            'when(vbObj unless(ddDeleteObj(vbObj) error("cellview cleanup failed"))))'
        )
    _checked_skill_output(
        client,
        "progn(" + " ".join(commands) + ' "cleaned")',
        context=f"clean temporary schematics in {library}",
        timeout=timeout,
    )


def _install_staged_schematic(
    client: Any,
    library: str,
    staging_cell: str,
    target_cell: str,
    *,
    overwrite: bool,
    timeout: int,
) -> tuple[str, str | None]:
    backup_cell = f"__vb_bak_{uuid.uuid4().hex[:16]}"
    overwrite_expr = "t" if overwrite else "nil"
    skill = (
        "let((vbStageCv vbTargetObj vbTargetCv vbBackupCv vbBackupObj "
        "vbReplacing vbAction vbAttempt vbRollback) "
        f'vbTargetObj = ddGetObj({_q(library)} {_q(target_cell)} "schematic") '
        "vbReplacing = if(vbTargetObj t nil) "
        'vbAction = if(vbReplacing "replaced" "created") '
        "when(vbTargetObj ddReleaseObj(vbTargetObj) vbTargetObj = nil) "
        f'when(vbReplacing && !{overwrite_expr} error("target schematic exists")) '
        "unless(isCallable('dbCopyCellView) error(\"dbCopyCellView API unavailable\")) "
        f'when(dbFindOpenCellViewByName({_q(library)} {_q(target_cell)} "schematic") '
        'error("target schematic is open")) '
        f'vbBackupObj = ddGetObj({_q(library)} {_q(backup_cell)} "schematic") '
        'when(vbBackupObj unless(ddDeleteObj(vbBackupObj) error("stale backup delete failed"))) '
        "when(vbReplacing "
        f'vbTargetCv = dbOpenCellViewByType({_q(library)} {_q(target_cell)} '
        '"schematic" "schematic" "r") '
        'unless(vbTargetCv error("target backup source open failed")) '
        f'vbBackupCv = dbCopyCellView(vbTargetCv {_q(library)} {_q(backup_cell)} '
        '"schematic" nil nil nil) '
        'unless(vbBackupCv error("target schematic backup failed")) '
        'unless(dbClose(vbBackupCv) error("backup close failed")) '
        'vbBackupCv = nil '
        'unless(dbClose(vbTargetCv) error("target backup source close failed")) '
        'vbTargetCv = nil) '
        f'vbStageCv = dbOpenCellViewByType({_q(library)} {_q(staging_cell)} '
        '"schematic" "schematic" "r") '
        'unless(vbStageCv error("staged schematic open failed")) '
        "vbAttempt = errset(progn("
        f'vbTargetCv = dbCopyCellView(vbStageCv {_q(library)} {_q(target_cell)} '
        f'"schematic" nil nil {overwrite_expr}) '
        'unless(vbTargetCv error("staged schematic copy failed")) '
        'unless(dbClose(vbTargetCv) error("installed schematic close failed")) '
        'vbTargetCv = nil t) nil) '
        'when(vbStageCv dbClose(vbStageCv) vbStageCv = nil) '
        "unless(vbAttempt && car(vbAttempt) "
        "if(vbReplacing "
        "then vbRollback = errset(progn("
        f'vbBackupCv = dbOpenCellViewByType({_q(library)} {_q(backup_cell)} '
        '"schematic" "schematic" "r") '
        'unless(vbBackupCv error("backup open failed")) '
        f'vbTargetCv = dbCopyCellView(vbBackupCv {_q(library)} {_q(target_cell)} '
        '"schematic" nil nil t) '
        'unless(vbTargetCv error("rollback copy failed")) '
        'dbClose(vbTargetCv) vbTargetCv = nil '
        'dbClose(vbBackupCv) vbBackupCv = nil t) nil) '
        "else "
        f'vbTargetObj = ddGetObj({_q(library)} {_q(target_cell)} "schematic") '
        'vbRollback = if(vbTargetObj errset(ddDeleteObj(vbTargetObj) nil) list(t))) '
        'unless(vbRollback && car(vbRollback) '
        f'error("install failed and rollback failed; backup retained as {escape_skill_string(library)}/{escape_skill_string(backup_cell)}")) '
        "when(vbReplacing "
        f'vbBackupObj = ddGetObj({_q(library)} {_q(backup_cell)} "schematic") '
        'when(vbBackupObj unless(ddDeleteObj(vbBackupObj) '
        f'error("install failed; rollback succeeded but backup cleanup failed: {escape_skill_string(library)}/{escape_skill_string(backup_cell)}")))) '
        'error("staged schematic install failed")) '
        'list("vbInstalled" vbAction if(vbReplacing '
        f'{_q(backup_cell)} nil)))'
    )
    output = _checked_skill_output(
        client,
        skill,
        context=f"install staged schematic {library}/{target_cell}",
        timeout=timeout,
    )
    if not is_single_complete_skill_list(output):
        raise RuntimeError(f"invalid install response for {library}/{target_cell}: {output!r}")
    parsed = parse_sexpr(output)
    if (
        not isinstance(parsed, list)
        or len(parsed) != 3
        or parsed[0] != "vbInstalled"
        or parsed[1] not in {"created", "replaced"}
    ):
        raise RuntimeError(f"invalid install response for {library}/{target_cell}: {output!r}")
    return str(parsed[1]), str(parsed[2]) if parsed[2] is not None else None


def _rollback_installed_schematic(
    client: Any,
    library: str,
    target_cell: str,
    action: str,
    backup_cell: str | None,
    *,
    timeout: int,
) -> None:
    if action == "replaced":
        if not backup_cell:
            raise RuntimeError("cannot roll back replaced schematic without backup")
        body = (
            "let((vbBackupCv vbTargetCv) "
            f'vbBackupCv = dbOpenCellViewByType({_q(library)} {_q(backup_cell)} '
            '"schematic" "schematic" "r") '
            'unless(vbBackupCv error("rollback backup open failed")) '
            f'vbTargetCv = dbCopyCellView(vbBackupCv {_q(library)} {_q(target_cell)} '
            '"schematic" nil nil t) '
            'unless(vbTargetCv error("rollback copy failed")) '
            'dbClose(vbTargetCv) dbClose(vbBackupCv) "rolledBack")'
        )
    else:
        body = (
            "let((vbObj) "
            f'vbObj = ddGetObj({_q(library)} {_q(target_cell)} "schematic") '
            'when(vbObj unless(ddDeleteObj(vbObj) error("created target rollback failed"))) '
            '"rolledBack")'
        )
    _checked_skill_output(
        client,
        body,
        context=f"roll back {library}/{target_cell}",
        timeout=timeout,
    )


def _check_result_dict(result: Any) -> dict[str, Any]:
    data = asdict(result)
    if data.get("screenshot") is not None:
        data["screenshot"] = str(data["screenshot"])
    return data


def import_manifest_circuit(
    client: Any,
    source: Mapping[str, Any],
    process_map: Mapping[str, Any],
    process: str,
    *,
    verify: bool = True,
    overwrite: bool = False,
    timeout: int = 180,
    terminal_clearance_in_grid_units: float = 6.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stage, check, verify, and transactionally install one circuit."""

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
    if _cellview_exists(client, library, cell, timeout=min(timeout, 30)) and not overwrite:
        raise FileExistsError(
            f"target schematic {library}/{cell} exists; pass overwrite=True to replace it"
        )
    staging_cell = f"__vb_stage_{uuid.uuid4().hex[:16]}"

    commands: list[str] = []
    commands.extend(
        f'unless({_create_net(str(name))} error("net creation failed: {escape_skill_string(str(name))}"))'
        for name in prepared["nets"]
    )
    for item in prepared["instances"]:
        reference = str(item["reference"])
        xy = _point_to_uu(layout["placements"][reference], grid)
        commands.append(
            _create_instance(item, xy, str(layout["orientations"][reference]))
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
        commands.append(_create_pin(str(port["name"]), xy, pin_orientation(port)))

    expanded_supply_labels: list[str] = []
    for item in prepared["instances"]:
        if item.get("sourceInvocationKind") != "expanded-subcircuit":
            continue
        source_node = next(
            (node for node in item["nodes"] if node["sourcePinName"] == "S"), None
        )
        if source_node is None:
            continue
        mapped_pin = str(source_node["pinName"])
        commands.append(
            _label_instance_term_at_center(
                str(item["reference"]),
                mapped_pin,
                str(source_node["netName"]),
            )
        )
        expanded_supply_labels.append(
            f'{item["reference"]}.{mapped_pin}={source_node["netName"]}'
        )
    commands.append('unless(dbSave(cv) error("staged schematic save failed"))')
    operation_count = len(commands) + 2
    build_skill = (
        "let((cv vbBuildResult) "
        f"{open_cell_view(library, staging_cell, view='schematic', mode='w')} "
        'unless(cv error("staged schematic open failed")) '
        "vbBuildResult = unwindProtect(progn("
        + " ".join(commands)
        + ') when(cv unless(dbClose(cv) error("staged schematic close failed")) '
        "cv = nil)) vbBuildResult)"
    )

    started = time.monotonic()
    try:
        response = client.execute_operations([build_skill], timeout=timeout)
        ensure_operation_response(
            response, context=f"build staged schematic {library}/{staging_cell}"
        )
    except Exception:
        try:
            _delete_cellviews(
                client, library, [staging_cell], timeout=min(timeout, 30)
            )
        except Exception:
            pass
        raise

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
        "operationCount": operation_count,
        "geometryAudit": layout["geometryAudit"],
        "routingAdjustments": routing_adjustments,
        "expandedSupplyLabels": expanded_supply_labels,
        "warnings": warnings,
        "overwrite": overwrite,
    }
    for key in ("galleryId", "galleryName", "sourceId", "sourceName"):
        if key in prepared:
            result[key] = prepared[key]

    stage_result = dict(result)
    stage_result["cellName"] = staging_cell
    action: str | None = None
    backup_cell: str | None = None
    try:
        first_check = check_and_save_schematic(
            client, library, staging_cell, timeout=min(timeout, 120)
        )
        result["checkPasses"] = [_check_result_dict(first_check)]
        if not first_check.ok:
            raise RuntimeError(
                f"{library}/{cell} staged schCheck failed with "
                f"{first_check.check_error_count} error(s): "
                + "; ".join(item.message for item in first_check.errors)
            )
        first_verification = None
        if verify:
            first_verification = verify_manifest_circuit(
                client,
                prepared,
                stage_result,
                process_map,
                layout=layout,
                timeout=min(timeout, 120),
            )
            if not first_verification["passed"]:
                raise RuntimeError(
                    f"{library}/{cell} staged verification failed: "
                    + "; ".join(first_verification["errors"])
                )

        second_check = check_and_save_schematic(
            client, library, staging_cell, timeout=min(timeout, 120)
        )
        result["checkPasses"].append(_check_result_dict(second_check))
        if not second_check.ok:
            raise RuntimeError(
                f"{library}/{cell} second staged schCheck failed with "
                f"{second_check.check_error_count} error(s): "
                + "; ".join(item.message for item in second_check.errors)
            )
        if verify:
            second_verification = verify_manifest_circuit(
                client,
                prepared,
                stage_result,
                process_map,
                layout=layout,
                timeout=min(timeout, 120),
            )
            if second_verification != first_verification:
                raise RuntimeError(
                    f"{library}/{cell} topology changed after the second schCheck"
                )
            result["stagedVerification"] = second_verification

        action, backup_cell = _install_staged_schematic(
            client,
            library,
            staging_cell,
            cell,
            overwrite=overwrite,
            timeout=min(timeout, 120),
        )
        result["action"] = action
        final_check = check_and_save_schematic(
            client, library, cell, timeout=min(timeout, 120)
        )
        result["finalCheck"] = _check_result_dict(final_check)
        if not final_check.ok:
            raise RuntimeError(
                f"{library}/{cell} installed schCheck failed with "
                f"{final_check.check_error_count} error(s): "
                + "; ".join(item.message for item in final_check.errors)
            )
        if verify:
            result["verification"] = verify_manifest_circuit(
                client,
                prepared,
                result,
                process_map,
                layout=layout,
                timeout=min(timeout, 120),
            )
            if result["verification"] != result["stagedVerification"]:
                raise RuntimeError(
                    f"{library}/{cell} installed readback differs from staged readback"
                )
            if not result["verification"]["passed"]:
                raise RuntimeError(
                    f"{library}/{cell} installed verification failed: "
                    + "; ".join(result["verification"]["errors"])
                )
    except Exception as failure:
        rollback_failure: Exception | None = None
        if action is not None:
            try:
                _rollback_installed_schematic(
                    client,
                    library,
                    cell,
                    action,
                    backup_cell,
                    timeout=min(timeout, 120),
                )
            except Exception as exc:
                rollback_failure = exc
        if rollback_failure is None:
            try:
                _delete_cellviews(
                    client,
                    library,
                    [staging_cell, backup_cell],
                    timeout=min(timeout, 30),
                )
            except Exception:
                pass
            raise
        raise RuntimeError(
            f"{failure}; rollback also failed: {rollback_failure}; "
            f"backup retained as {library}/{backup_cell}"
        ) from failure

    try:
        _delete_cellviews(
            client,
            library,
            [staging_cell, backup_cell],
            timeout=min(timeout, 30),
        )
    except Exception as exc:
        result["cleanupWarnings"] = [str(exc)]
    result["elapsedSeconds"] = round(time.monotonic() - started, 3)
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
    layout: Mapping[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Read a generated cell back and verify topology, placement, and CDFs."""

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
        if layout is not None:
            grid = float(process_map["gridUnit"])
            expected_xy = [
                float(value) * grid
                for value in layout["placements"][reference]
            ]
            actual_xy = actual.get("xy")
            if not isinstance(actual_xy, list) or len(actual_xy) != 2 or not all(
                math.isclose(float(got), want, rel_tol=0.0, abs_tol=1e-9)
                for got, want in zip(actual_xy, expected_xy)
            ):
                errors.append(f"{reference} xy {actual_xy!r} != {expected_xy!r}")
            expected_orient = str(layout["orientations"][reference])
            if actual.get("orient") != expected_orient:
                errors.append(
                    f"{reference} orient {actual.get('orient')!r} != {expected_orient!r}"
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
    expected_port_nets = {
        str(item["name"]): str(item["netName"])
        for item in prepared_source["ports"]
    }
    bad_port_nets = sorted(
        f"{name}:{data['pins'][name].get('net')!r}!={net!r}"
        for name, net in expected_port_nets.items()
        if name in data["pins"] and data["pins"][name].get("net") != net
    )
    if bad_port_nets:
        errors.append(f"port-net mismatches {bad_port_nets}")
    if layout is not None:
        expected_occurrences = Counter(str(item["name"]) for item in layout["ports"])
        bad_occurrences = sorted(
            f"{name}:{data['pins'].get(name, {}).get('occurrences', 0)}!={count}"
            for name, count in expected_occurrences.items()
            if data["pins"].get(name, {}).get("occurrences", 0) != count
        )
        if bad_occurrences:
            errors.append(f"port occurrence mismatches {bad_occurrences}")
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
        "placementsChecked": layout is not None,
    }


def import_schematic_manifest(
    client: Any,
    manifest: JsonSource,
    process_map: JsonSource,
    *,
    processes: Sequence[str] | None = None,
    cells: Sequence[str] | None = None,
    verify: bool = True,
    validate_masters: bool = True,
    overwrite: bool = False,
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
    targets = [
        (str(map_data["processes"][process]["outputLibrary"]), str(source["cellName"]))
        for process in selected_processes
        for source in selected_circuits
    ]
    collisions = sorted(target for target, count in Counter(targets).items() if count > 1)
    if collisions:
        raise ValueError(f"multiple imports resolve to the same output target: {collisions}")

    master_checks = (
        validate_process_master_offsets(client, map_data, selected_processes)
        if validate_masters
        else []
    )
    imported: list[dict[str, Any]] = []
    for process in selected_processes:
        for source in selected_circuits:
            _prepared, result = import_manifest_circuit(
                client,
                source,
                map_data,
                process,
                verify=verify,
                overwrite=overwrite,
                timeout=timeout,
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

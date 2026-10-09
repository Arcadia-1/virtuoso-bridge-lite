from __future__ import annotations

import copy
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.schematic import (
    capture_schematic_cell,
    import_schematic_manifest,
    load_process_map,
    load_schematic_manifest,
    orient_offset,
    plan_manifest_circuit,
    prepare_schematic_for_process,
    source_orientation,
    validate_process_master_offsets,
)
from virtuoso_bridge.virtuoso.ops import skill_point, skill_point_list
from virtuoso_bridge.virtuoso.schematic import manifest as manifest_module
from virtuoso_bridge.virtuoso.schematic.diagnostics import SchematicCheckSaveResult


def _manifest() -> dict:
    return {
        "schema": "virtuoso-bridge-exact-schematic-v1",
        "circuits": [
            {
                "cellName": "one_transistor",
                "instances": [
                    {
                        "id": "source-M0",
                        "reference": "M0",
                        "deviceClass": "mos",
                        "kind": "nmos",
                        "sourceTarget": "generic_nmos",
                        "sourcePosition": {"x": 50, "y": 50},
                        "sourceTransform": {"rotation": 0, "mirror": "none"},
                        "nodes": [
                            {"sourcePinName": "D", "pinName": "D", "netName": "D"},
                            {"sourcePinName": "G", "pinName": "G", "netName": "G"},
                            {"sourcePinName": "S", "pinName": "S", "netName": "S"},
                            {"sourcePinName": "B", "pinName": "B", "netName": "B"},
                        ],
                        "sourceParameters": {"l": "180n", "w": "1u", "m": "1"},
                    }
                ],
                "ports": [
                    {"name": name, "netName": name, "direction": "inout"}
                    for name in ("D", "G", "S", "B")
                ],
                "nets": ["D", "G", "S", "B"],
                "sourceGeometry": {
                    "bounds": {"minX": 20, "minY": 20, "maxX": 80, "maxY": 80},
                    "portOccurrences": [
                        {
                            "occurrenceId": "PD",
                            "name": "D",
                            "netName": "D",
                            "direction": "inout",
                            "sourceSymbolId": "port",
                            "sourcePosition": {"x": 54, "y": 20},
                            "sourceTransform": {"rotation": 90, "mirror": "none"},
                        },
                        {
                            "occurrenceId": "PG",
                            "name": "G",
                            "netName": "G",
                            "direction": "inout",
                            "sourceSymbolId": "port",
                            "sourcePosition": {"x": 20, "y": 50},
                            "sourceTransform": {"rotation": 0, "mirror": "none"},
                        },
                        {
                            "occurrenceId": "PS",
                            "name": "S",
                            "netName": "S",
                            "direction": "inout",
                            "sourceSymbolId": "port",
                            "sourcePosition": {"x": 54, "y": 80},
                            "sourceTransform": {"rotation": 270, "mirror": "none"},
                        },
                        {
                            "occurrenceId": "PB",
                            "name": "B",
                            "netName": "B",
                            "direction": "inout",
                            "sourceSymbolId": "port",
                            "sourcePosition": {"x": 80, "y": 51},
                            "sourceTransform": {"rotation": 0, "mirror": "horizontal"},
                        },
                    ],
                    "junctions": [],
                    "contacts": [],
                    "routes": [
                        {
                            "id": f"R{index}",
                            "netName": net,
                            "start": {
                                "kind": "port",
                                "instanceId": port,
                                "pinName": net,
                                "sourcePoint": start,
                            },
                            "steps": [
                                {
                                    "kind": "instance",
                                    "instanceId": "source-M0",
                                    "pinName": net,
                                    "sourcePoint": end,
                                }
                            ],
                        }
                        for index, (net, port, start, end) in enumerate(
                            (
                                ("D", "PD", {"x": 54, "y": 20}, {"x": 54, "y": 47}),
                                ("G", "PG", {"x": 20, "y": 50}, {"x": 50, "y": 50}),
                                ("S", "PS", {"x": 54, "y": 80}, {"x": 54, "y": 53}),
                                ("B", "PB", {"x": 80, "y": 51}, {"x": 54, "y": 51}),
                            )
                        )
                    ],
                    "localBulkLabels": [],
                    "localBulkShorts": [],
                    "annotationStubs": [],
                },
            }
        ],
    }


def _process_map() -> dict:
    return {
        "schema": "virtuoso-bridge-process-map-v1",
        "gridUnit": 0.0625,
        "sourceScaleInGridUnits": 1,
        "originInGridUnits": [0, 0],
        "localLabelStubInGridUnits": 4,
        "processes": {
            "demo180": {
                "outputLibrary": "generated_demo180",
                "devices": {
                    "nmos": {
                        "library": "demoPdk",
                        "cell": "nmos4",
                        "view": "symbol",
                        "pinMap": {"D": "D", "G": "G", "S": "S", "B": "B"},
                        "pinOffsets": {
                            "D": [4, 3],
                            "G": [0, 0],
                            "S": [4, -3],
                            "B": [4, -1],
                        },
                        "parameterMap": {"l": "l", "w": "w", "m": "m"},
                    }
                },
            }
        },
    }


def test_transform_mapping_is_explicit_and_complete() -> None:
    assert source_orientation({"rotation": 0, "mirror": "none"}) == "R0"
    assert source_orientation({"rotation": 0, "mirror": "horizontal"}) == "MY"
    assert source_orientation({"rotation": 90, "mirror": "vertical"}) == "MXR90"
    assert orient_offset((4, 3), "MY") == (-4, 3)
    with pytest.raises(ValueError, match="unsupported source transform"):
        source_orientation({"rotation": 45, "mirror": "none"})
    with pytest.raises(ValueError, match="unsupported source rotation"):
        source_orientation({"rotation": 90.5, "mirror": "none"})


def test_skill_points_preserve_sub_milligrid_precision() -> None:
    assert skill_point(2.145833333, 2.078125) == "'(2.145833333 2.078125)"
    assert skill_point_list([(0.0, -0.0), (0.0625, 0.03125)]) == (
        "'((0.000 0.000) (0.0625 0.03125))"
    )


def test_manifest_planning_preserves_exact_source_axis_relations() -> None:
    prepared, layout = plan_manifest_circuit(
        _manifest()["circuits"][0], _process_map(), "demo180"
    )

    assert prepared["instances"][0]["targetCell"] == "nmos4"
    assert layout["placements"]["M0"] == (30.0, 30.0)
    assert layout["anchors"]["M0"] == {
        "D": (34.0, 33.0),
        "G": (30.0, 30.0),
        "S": (34.0, 27.0),
        "B": (34.0, 29.0),
    }
    assert layout["geometryAudit"]["afterViolationCount"] == 0
    assert layout["geometryAudit"]["horizontalEdges"] == 2
    assert layout["geometryAudit"]["verticalEdges"] == 2


def test_planning_rejects_endpoint_on_the_wrong_net() -> None:
    manifest = _manifest()
    manifest["circuits"][0]["sourceGeometry"]["routes"][0]["netName"] = "G"

    with pytest.raises(ValueError, match="ambiguous source endpoint"):
        plan_manifest_circuit(
            manifest["circuits"][0], _process_map(), "demo180"
        )


def test_manifest_and_process_map_validation_reject_ambiguous_input() -> None:
    duplicate = _manifest()
    duplicate["circuits"].append(copy.deepcopy(duplicate["circuits"][0]))
    with pytest.raises(ValueError, match="duplicate circuit"):
        load_schematic_manifest(duplicate)

    bad_map = _process_map()
    bad_map["gridUnit"] = 0
    with pytest.raises(ValueError, match="gridUnit"):
        load_process_map(bad_map)

    bad_rotation = _manifest()
    bad_rotation["circuits"][0]["instances"][0]["sourceTransform"]["rotation"] = 90.5
    with pytest.raises(ValueError, match="multiple of 90"):
        load_schematic_manifest(bad_rotation)


def test_pin_map_is_applied_to_bulk_geometry() -> None:
    manifest = _manifest()
    geometry = manifest["circuits"][0]["sourceGeometry"]
    geometry["localBulkLabels"] = [
        {"reference": "M0", "pinName": "B", "name": "B", "netName": "B"}
    ]
    geometry["localBulkShorts"] = [
        {
            "reference": "M0",
            "bulkPinName": "B",
            "sourcePinName": "S",
            "netName": "B",
        }
    ]
    process_map = _process_map()
    device = process_map["processes"]["demo180"]["devices"]["nmos"]
    device["pinMap"] = {"D": "d", "G": "g", "S": "s", "B": "bulk"}
    device["pinOffsets"] = {
        "d": [4, 3],
        "g": [0, 0],
        "s": [4, -3],
        "bulk": [4, -1],
    }

    prepared = prepare_schematic_for_process(
        manifest["circuits"][0], process_map, "demo180"
    )

    assert geometry["localBulkLabels"][0]["pinName"] == "B"
    assert prepared["sourceGeometry"]["localBulkLabels"][0]["pinName"] == "bulk"
    assert prepared["sourceGeometry"]["localBulkShorts"][0]["bulkPinName"] == "bulk"
    assert prepared["sourceGeometry"]["localBulkShorts"][0]["sourcePinName"] == "s"


class _FakeClient:
    def __init__(self) -> None:
        self.operations: list[str] = []
        self.skill: list[str] = []
        self.screenshot_target = None

    def execute_skill(self, skill: str, timeout: int = 60):
        self.skill.append(skill)
        if "techGetDBUPerUU" in skill:
            return SimpleNamespace(errors=[], output="160.0")
        if 'list("vbExists"' in skill:
            return SimpleNamespace(errors=[], output='("vbExists" nil)')
        if 'list("vbCheckSave"' in skill:
            return SimpleNamespace(
                errors=[], output='("vbCheckSave" 0 0 t nil nil nil)'
            )
        if 'list("vbInstalled"' in skill:
            return SimpleNamespace(
                errors=[], output='("vbInstalled" "created" nil)'
            )
        return SimpleNamespace(errors=[], output="t")

    def execute_operations(self, operations: list[str], timeout: int = 60):
        self.operations = list(operations)
        return {"ok": True, "result": {"status": "success", "errors": []}}

    def open_window(self, lib: str, cell: str, view: str, timeout: int = 60):
        return SimpleNamespace(errors=[], output="window:42")

    def screenshot(self, output: Path, target: int, timeout: int = 60):
        self.screenshot_target = target
        return SimpleNamespace(errors=[], output=str(output))


def test_import_manifest_emits_named_connectivity_and_visible_wires() -> None:
    client = _FakeClient()

    result = import_schematic_manifest(
        client,
        _manifest(),
        _process_map(),
        processes=["demo180"],
        verify=False,
        validate_masters=False,
    )

    assert result["passed"]
    assert result["circuits"][0]["wireCommands"] == 4
    script = "\n".join(client.operations)
    assert 'dbCreateInst(cv vbMaster "M0"' in script
    assert 'dbCreateNet(cv "G")' in script
    assert 'rbTerm = car(setof(x rbInst~>master~>terminals x~>name == "G"))' in script
    assert 'schCreateWire(cv "route" "full"' in script
    assert 'schCreatePin(cv' in script
    assert 'unless(dbSave(cv) error("staged schematic save failed"))' in script
    assert 'dbClose(cv)' in script
    assert "unwindProtect" in script
    assert len(result["circuits"][0]["checkPasses"]) == 2
    assert result["circuits"][0]["finalCheck"]["status"] == "saved"


def test_import_refuses_existing_target_by_default() -> None:
    class ExistingClient(_FakeClient):
        def execute_skill(self, skill: str, timeout: int = 60):
            if 'list("vbExists"' in skill:
                return SimpleNamespace(errors=[], output='("vbExists" t)')
            return super().execute_skill(skill, timeout)

    with pytest.raises(FileExistsError, match="overwrite=True"):
        import_schematic_manifest(
            ExistingClient(),
            _manifest(),
            _process_map(),
            processes=["demo180"],
            verify=False,
            validate_masters=False,
        )


def test_import_rejects_cross_process_output_collisions_before_contacting_client() -> None:
    process_map = _process_map()
    process_map["processes"]["demo28"] = copy.deepcopy(
        process_map["processes"]["demo180"]
    )

    with pytest.raises(ValueError, match="same output target"):
        import_schematic_manifest(
            _FakeClient(),
            _manifest(),
            process_map,
            verify=False,
            validate_masters=False,
        )


def test_cdf_updates_restore_cell_values_and_propagate_callback_failure() -> None:
    client = _FakeClient()
    import_schematic_manifest(
        client,
        _manifest(),
        _process_map(),
        processes=["demo180"],
        verify=False,
        validate_masters=False,
    )
    script = "\n".join(client.operations)
    assert "rbUpdateAttempt = errset(unwindProtect" in script
    assert "CDF callback failed: l" in script
    assert "foreach(rbParam rbCCDF~>parameters putpropq" in script


def test_failed_final_check_rolls_back_existing_target(monkeypatch) -> None:
    class ReplacingClient(_FakeClient):
        def execute_skill(self, skill: str, timeout: int = 60):
            self.skill.append(skill)
            if "techGetDBUPerUU" in skill:
                return SimpleNamespace(errors=[], output="160.0")
            if 'list("vbExists"' in skill:
                return SimpleNamespace(errors=[], output='("vbExists" t)')
            if 'list("vbInstalled"' in skill:
                backup = re.search(r'"(__vb_bak_[0-9a-f]+)"', skill)
                assert backup is not None
                return SimpleNamespace(
                    errors=[],
                    output=f'("vbInstalled" "replaced" "{backup.group(1)}")',
                )
            return SimpleNamespace(errors=[], output='"ok"')

    calls = 0

    def fake_check(_client, lib, cell, **_kwargs):
        nonlocal calls
        calls += 1
        final = calls == 3
        return SchematicCheckSaveResult(
            status="check_failed" if final else "saved",
            lib=lib,
            cell=cell,
            view="schematic",
            checked=True,
            saved=True,
            check_error_count=1 if final else 0,
            check_warning_count=0,
            log_capture_available=False,
        )

    monkeypatch.setattr(manifest_module, "check_and_save_schematic", fake_check)
    client = ReplacingClient()

    with pytest.raises(RuntimeError, match="installed schCheck failed"):
        import_schematic_manifest(
            client,
            _manifest(),
            _process_map(),
            processes=["demo180"],
            verify=False,
            validate_masters=False,
            overwrite=True,
        )

    assert any("rollback backup open failed" in skill for skill in client.skill)


def test_live_master_offsets_are_checked_before_import() -> None:
    class Client(_FakeClient):
        def execute_skill(self, skill: str, timeout: int = 60):
            return SimpleNamespace(
                errors=[],
                output='"D|0.25|0.1875\\nG|0|0\\nS|0.25|-0.1875\\nB|0.25|-0.0625\\n"',
            )

    checks = validate_process_master_offsets(Client(), _process_map(), ["demo180"])
    assert checks == [
        {
            "master": "demoPdk/nmos4",
            "pinOffsets": {"D": [4, 3], "G": [0, 0], "S": [4, -3], "B": [4, -1]},
        }
    ]


def test_live_master_offsets_preserve_fractional_grid_units() -> None:
    process_map = _process_map()
    process_map["processes"]["demo180"]["devices"]["nmos"]["pinOffsets"]["D"] = [
        4.5,
        3.25,
    ]

    class Client(_FakeClient):
        def execute_skill(self, skill: str, timeout: int = 60):
            return SimpleNamespace(
                errors=[],
                output='"D|0.28125|0.203125\\nG|0|0\\nS|0.25|-0.1875\\nB|0.25|-0.0625\\n"',
            )

    checks = validate_process_master_offsets(Client(), process_map, ["demo180"])
    assert checks[0]["pinOffsets"]["D"] == [4.5, 3.25]


def test_capture_targets_the_window_returned_by_open_window(tmp_path: Path) -> None:
    client = _FakeClient()
    output = tmp_path / "demo.png"

    assert capture_schematic_cell(client, "LIB", "CELL", output) == output
    assert client.screenshot_target == 42
    assert any("w~>windowNum == 42" in skill for skill in client.skill)


@pytest.mark.parametrize("shift,expected", [(1 / 3, 30.3), (-30.35, -0.3), (0.05, 30.1)])
def test_native_grid_snaps_fractional_solver_positions_and_attached_wires(shift, expected) -> None:
    _, layout = plan_manifest_circuit(_manifest()["circuits"][0], _process_map(), "demo180")
    # The least-squares translation can be fractional even with integral offsets.
    for key in ("placements",):
        layout[key] = {name: (p[0] + shift, p[1] + shift) for name, p in layout[key].items()}
    for pins in layout["anchors"].values():
        for name, p in pins.items():
            pins[name] = (p[0] + shift, p[1] + shift)
    for key in ("routes", "contacts", "expandedLinks", "bulkStubs", "bulkShorts", "annotationStubs"):
        for row in layout[key]:
            row["points"] = [(p[0] + shift, p[1] + shift) for p in row["points"]]
    for row in layout["ports"] + layout["junctions"]:
        row["xy"] = (row["xy"][0] + shift, row["xy"][1] + shift)
    manifest_module._snap_layout_to_database_grid(layout, 0.0625, 160)
    assert layout["placements"]["M0"] == pytest.approx((expected, expected))
    assert layout["anchors"]["M0"]["G"] == layout["placements"]["M0"]
    assert layout["geometryAudit"]["databaseGrid"]["maxShiftInUserUnits"] <= 0.5 / 160 + 1e-12
    coordinates = list(layout["placements"].values())
    coordinates += [p for row in layout["routes"] for p in row["points"]]
    assert all(abs(v * 10 - round(v * 10)) < 1e-8 for p in coordinates for v in p)
    again = copy.deepcopy(layout)
    manifest_module._snap_layout_to_database_grid(again, 0.0625, 160)
    assert again["placements"] == layout["placements"]
    assert again["routes"] == layout["routes"]


@pytest.mark.parametrize("dbu", [0, -1, float("inf"), float("nan")])
def test_native_grid_rejects_invalid_resolution(dbu) -> None:
    with pytest.raises(ValueError, match="database units"):
        manifest_module._snap_layout_to_database_grid({}, 0.0625, dbu)


def test_native_grid_rejects_collapsed_geometry_without_mutating_layout() -> None:
    _, layout = plan_manifest_circuit(_manifest()["circuits"][0], _process_map(), "demo180")
    layout["routes"][0]["points"] = [(0, 0), (0.01, 0)]
    original = copy.deepcopy(layout)
    with pytest.raises(ValueError, match="collapses"):
        manifest_module._snap_layout_to_database_grid(layout, 0.0625, 160)
    assert layout == original


def test_native_grid_rejects_unrepresentable_master_offsets() -> None:
    _, layout = plan_manifest_circuit(_manifest()["circuits"][0], _process_map(), "demo180")
    x, y = layout["anchors"]["M0"]["D"]
    layout["anchors"]["M0"]["D"] = (x + 0.01, y)
    with pytest.raises(ValueError, match="master pin offset"):
        manifest_module._snap_layout_to_database_grid(layout, 0.0625, 160)


def test_unknown_native_grid_fails_before_any_creation() -> None:
    class UnknownGrid(_FakeClient):
        def execute_skill(self, skill, timeout=60):
            if "techGetDBUPerUU" in skill:
                return SimpleNamespace(errors=[], output="nil")
            return super().execute_skill(skill, timeout)
    client = UnknownGrid()
    with pytest.raises(ValueError, match="cannot determine schematic database units"):
        import_schematic_manifest(client, _manifest(), _process_map(), verify=False, validate_masters=False)
    assert client.operations == []


def _rail_manifest() -> dict:
    data = _manifest()
    geometry = data["circuits"][0]["sourceGeometry"]
    geometry["junctions"].append({
        "id": "rail-tail", "netName": "D", "role": "route-anchor",
        "sourcePosition": {"x": 44, "y": 20},
    })
    geometry["routes"].append({
        "id": "rail", "netName": "D", "presentation": "power-rail",
        "start": {"kind": "junction", "junctionId": "rail-tail", "sourcePoint": {"x": 44, "y": 20}},
        "steps": [copy.deepcopy(geometry["routes"][0]["start"])],
    })
    return data


def _plan_rail(data):
    return plan_manifest_circuit(data["circuits"][0], _process_map(), "demo180")


@pytest.mark.parametrize("reverse", [False, True])
def test_display_rail_keeps_geometry_and_does_not_mutate_source(reverse) -> None:
    data = _rail_manifest()
    rail = data["circuits"][0]["sourceGeometry"]["routes"][-1]
    if reverse:
        rail["start"], rail["steps"][0] = rail["steps"][0], rail["start"]
    source, layout = _plan_rail(data)
    original = copy.deepcopy((source, layout))
    display = manifest_module._display_only_power_rails(source, layout)
    assert [row["id"] for row in display] == ["rail"]
    assert display[0]["points"] == layout["routes"][-1]["points"]
    assert (source, layout) == original


@pytest.mark.parametrize("case", ["signal", "branch", "label", "contact", "port", "pin", "crossing", "overlap", "floating"])
def test_display_rail_does_not_hide_electrical_connections_or_ambiguity(case) -> None:
    source, layout = _plan_rail(_rail_manifest())
    geometry = source["sourceGeometry"]
    rail = geometry["routes"][-1]
    a, b = layout["routes"][-1]["points"]
    midpoint = ((a[0] + b[0]) / 2, a[1])
    if case == "signal":
        rail["presentation"] = "signal"
    elif case == "branch":
        geometry["junctions"][-1]["role"] = "branch"
    elif case == "label":
        geometry["annotationStubs"].append({"anchorJunctionId": "rail-tail"})
    elif case == "contact":
        geometry["contacts"].append({"endpoints": [rail["start"]]})
    elif case == "port":
        layout["ports"].append({"xy": a})
    elif case == "pin":
        layout["anchors"]["M0"]["G"] = midpoint
    elif case in ("crossing", "overlap"):
        points = [(midpoint[0], a[1] - 1), (midpoint[0], a[1] + 1)] if case == "crossing" else [a, b]
        layout["routes"].append({"id": "other", "netName": "G", "points": points})
    elif case == "floating":
        layout["routes"] = layout["routes"][-1:]
    assert manifest_module._display_only_power_rails(source, layout) == []


def test_display_rails_cannot_support_each_other_on_a_floating_island() -> None:
    source, layout = _plan_rail(_rail_manifest())
    geometry = source["sourceGeometry"]
    geometry["junctions"] = [
        {"id": name, "role": "route-anchor"} for name in ("left", "join", "right")
    ]
    geometry["routes"] = [
        {"id": name, "netName": "D", "presentation": "power-rail",
         "start": {"kind": "junction", "junctionId": start},
         "steps": [{"kind": "junction", "junctionId": end}]}
        for name, start, end in (("left-rail", "left", "join"), ("right-rail", "join", "right"))
    ]
    geometry["contacts"] = []
    geometry["annotationStubs"] = []
    layout["routes"] = [
        {"id": "left-rail", "netName": "D", "points": [(100, 100), (110, 100)]},
        {"id": "right-rail", "netName": "D", "points": [(110, 100), (120, 100)]},
    ]
    for key in ("contacts", "expandedLinks", "bulkStubs", "bulkShorts", "annotationStubs"):
        layout[key] = []
    assert manifest_module._display_only_power_rails(source, layout) == []


def test_native_import_uses_unbound_drawing_for_display_rail() -> None:
    client = _FakeClient()
    result = import_schematic_manifest(client, _rail_manifest(), _process_map(),
                                       verify=False, validate_masters=False)
    command = client.operations[0]
    assert 'dbCreateLine(cv list("annotate" "drawing")' in command
    assert 'dbReplaceProp(vbRail "vbDisplayRailId" "string" "rail")' in command
    assert 'dbAddFigToNet(vbRail' not in command
    assert result["circuits"][0]["displayOnlyRails"][0]["id"] == "rail"


@pytest.mark.parametrize("corruption", ["none", "missing", "net", "position", "layer", "duplicate"])
def test_display_rail_readback_rejects_loss_or_electrical_binding(corruption) -> None:
    row = '("rail" "line" "annotate" "drawing" nil ((1.0 2.125) (3.0 2.125)))'
    if corruption == "net":
        row = row.replace('nil', '"D"')
    elif corruption == "position":
        row = row.replace('3.0', '4.0')
    elif corruption == "layer":
        row = row.replace('"annotate"', '"wire"')
    rows = "nil" if corruption == "missing" else f"({row}{' ' + row if corruption == 'duplicate' else ''})"

    class Client:
        def execute_skill(self, skill, timeout):
            assert 'dbClose(cv)' in skill
            return SimpleNamespace(errors=[], output=f'("vbDisplayRails" {rows})')

    result = {"library": "LIB", "cellName": "CELL", "displayOnlyRails": [
        {"id": "rail", "points": [[1.0, 2.125], [3.0, 2.125]]}]}
    errors = manifest_module._verify_display_rails(Client(), result, timeout=30)
    assert bool(errors) is (corruption != "none")

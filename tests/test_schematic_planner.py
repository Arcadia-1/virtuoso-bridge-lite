from __future__ import annotations

import pytest

from virtuoso_bridge.virtuoso.schematic import SchematicOps
from virtuoso_bridge.virtuoso.schematic.planner import (
    ConstraintLevel,
    ConstraintStrength,
    DeviceKind,
    DifferentialPairConstraint,
    GridPositionConstraint,
    SchematicInstanceSpec,
    SchematicPinSpec,
    SchematicPlanRequest,
    SchematicPlanner,
    SchematicPlannerConfig,
    SchematicPlanningError,
)


def _acceptance_request(*, reverse: bool = False) -> SchematicPlanRequest:
    instances = [
        SchematicInstanceSpec(
            "M_TAIL",
            "demoPdk",
            "nch",
            terminals={"D": "VS", "G": "BIAS", "S": "VSS", "B": "VSS"},
            kind=DeviceKind.NMOS,
        ),
        SchematicInstanceSpec(
            "M_INP",
            "demoPdk",
            "nch",
            terminals={"D": "VOP", "G": "VINP", "S": "VS", "B": "VSS"},
            kind=DeviceKind.NMOS,
        ),
        SchematicInstanceSpec(
            "M_INN",
            "demoPdk",
            "nch",
            terminals={"D": "VON", "G": "VINN", "S": "VS", "B": "VSS"},
            kind=DeviceKind.NMOS,
        ),
        SchematicInstanceSpec(
            "M_LOADP",
            "demoPdk",
            "pch",
            terminals={"D": "VOP", "G": "VON", "S": "VDD", "B": "VDD"},
            kind=DeviceKind.PMOS,
        ),
        SchematicInstanceSpec(
            "M_LOADN",
            "demoPdk",
            "pch",
            terminals={"D": "VON", "G": "VON", "S": "VDD", "B": "VDD"},
            kind=DeviceKind.PMOS,
        ),
        SchematicInstanceSpec(
            "M_OUT",
            "demoPdk",
            "nch",
            terminals={"D": "OUT", "G": "VOP", "S": "VSS", "B": "VSS"},
            kind=DeviceKind.NMOS,
            output_stage=True,
        ),
    ]
    pins = [
        SchematicPinSpec("VINP", "input", row=1),
        SchematicPinSpec("VINN", "input", row=2),
        SchematicPinSpec("OUT", "output", row=0),
    ]
    if reverse:
        instances.reverse()
        pins.reverse()
    return SchematicPlanRequest(
        instances=instances,
        pins=pins,
        positions=[GridPositionConstraint("M_TAIL", col=1.5, row=0)],
        differential_pairs=[
            DifferentialPairConstraint("M_INP", "M_INN", row=1, center_col=1.5),
            DifferentialPairConstraint("M_LOADP", "M_LOADN", row=2, center_col=1.5),
        ],
    )


def test_planner_builds_deterministic_differential_pair_acceptance_plan() -> None:
    planner = SchematicPlanner()

    forward = planner.plan(_acceptance_request())
    reversed_input = planner.plan(_acceptance_request(reverse=True))

    assert forward == reversed_input
    placements = {item.spec.name: item.placement for item in forward.instances}
    assert (placements["M_INP"].col, placements["M_INP"].row) == (1.0, 1.0)
    assert (placements["M_INN"].col, placements["M_INN"].row) == (2.0, 1.0)
    assert placements["M_INP"].orientation == "R0"
    assert placements["M_INN"].orientation == "MY"
    assert (placements["M_LOADP"].col, placements["M_LOADP"].row) == (1.0, 2.0)
    assert (placements["M_LOADN"].col, placements["M_LOADN"].row) == (2.0, 2.0)
    assert (placements["M_TAIL"].col, placements["M_TAIL"].row) == (1.5, 0.0)
    assert placements["M_OUT"].col == 5.0
    assert placements["M_OUT"].x == 7.5
    assert {pin.name: pin.col for pin in forward.pins} == {
        "OUT": -1.0,
        "VINN": -1.0,
        "VINP": -1.0,
    }


def test_hard_position_conflicting_with_hard_pair_is_reported() -> None:
    request = SchematicPlanRequest(
        instances=[
            SchematicInstanceSpec("ML", "pdk", "nch"),
            SchematicInstanceSpec("MR", "pdk", "nch"),
        ],
        positions=[GridPositionConstraint("ML", col=0, strength=ConstraintStrength.HARD)],
        differential_pairs=[
            DifferentialPairConstraint(
                "ML",
                "MR",
                center_col=2,
                separation=2,
                strength=ConstraintStrength.HARD,
            )
        ],
    )

    with pytest.raises(SchematicPlanningError) as caught:
        SchematicPlanner().plan(request)

    assert any(d.code == "hard_constraint_conflict" for d in caught.value.diagnostics)
    assert "ML.col" in str(caught.value)


def test_pair_without_row_adopts_a_hard_member_row() -> None:
    request = SchematicPlanRequest(
        instances=[
            SchematicInstanceSpec("ML", "pdk", "nch"),
            SchematicInstanceSpec("MR", "pdk", "nch"),
        ],
        positions=[GridPositionConstraint("ML", row=3)],
        differential_pairs=[DifferentialPairConstraint("ML", "MR")],
    )

    plan = SchematicPlanner().plan(request)

    assert plan.instance("ML").placement.row == 3
    assert plan.instance("MR").placement.row == 3


def test_soft_constraint_relaxation_is_not_silent() -> None:
    request = SchematicPlanRequest(
        instances=[
            SchematicInstanceSpec("ML", "pdk", "nch"),
            SchematicInstanceSpec("MR", "pdk", "nch"),
        ],
        positions=[
            GridPositionConstraint("ML", col=0, strength=ConstraintStrength.HARD),
        ],
        differential_pairs=[
            DifferentialPairConstraint(
                "ML",
                "MR",
                center_col=2,
                separation=2,
                strength=ConstraintStrength.SOFT,
            )
        ],
    )

    plan = SchematicPlanner().plan(request)

    assert plan.instance("ML").placement.col == 0
    assert any(
        d.level is ConstraintLevel.RELAXED and d.code == "soft_constraint_relaxed"
        for d in plan.diagnostics
    )


def test_hard_grid_collision_fails_instead_of_overlapping() -> None:
    request = SchematicPlanRequest(
        instances=[
            SchematicInstanceSpec("M0", "pdk", "nch"),
            SchematicInstanceSpec("M1", "pdk", "nch"),
        ],
        positions=[
            GridPositionConstraint("M0", col=1, row=1),
            GridPositionConstraint("M1", col=1, row=1),
        ],
    )

    with pytest.raises(SchematicPlanningError) as caught:
        SchematicPlanner().plan(request)

    assert any(d.code == "hard_grid_collision" for d in caught.value.diagnostics)


def test_soft_grid_collision_is_moved_and_reported() -> None:
    request = SchematicPlanRequest(
        instances=[
            SchematicInstanceSpec("M0", "pdk", "nch"),
            SchematicInstanceSpec("M1", "pdk", "nch"),
        ],
        positions=[
            GridPositionConstraint("M0", col=1, row=1, strength=ConstraintStrength.SOFT),
            GridPositionConstraint("M1", col=1, row=1, strength=ConstraintStrength.SOFT),
        ],
    )

    plan = SchematicPlanner().plan(request)

    locations = {(item.placement.col, item.placement.row) for item in plan.instances}
    assert len(locations) == 2
    assert any(d.code == "soft_grid_collision_resolved" for d in plan.diagnostics)


def test_request_from_readback_preserves_grid_positions_and_connectivity() -> None:
    data = {
        "instances": [
            {
                "name": "M0",
                "lib": "demoPdk",
                "cell": "nch",
                "view": "symbol",
                "xy": [3.0, 1.5],
                "orient": "MY",
                "terms": {"D": "OUT", "G": "IN", "S": "VSS", "B": "VSS"},
            }
        ],
        "pins": {"IN": {"direction": "input"}, "OUT": {"direction": "output"}},
    }

    request = SchematicPlanRequest.from_readback(data, grid_spacing=1.5)
    plan = SchematicPlanner(SchematicPlannerConfig(grid_spacing=1.5)).plan(request)

    instance = plan.instance("M0")
    assert (instance.placement.col, instance.placement.row) == (2.0, 1.0)
    assert instance.placement.orientation == "MY"
    assert dict(instance.spec.terminals) == data["instances"][0]["terms"]
    assert [(pin.name, pin.direction) for pin in plan.pins] == [
        ("IN", "input"),
        ("OUT", "output"),
    ]


def test_request_from_readback_requires_position_data() -> None:
    data = {
        "instances": [{"name": "M0", "lib": "pdk", "cell": "nch", "terms": {}}],
        "pins": {},
    }

    with pytest.raises(ValueError, match="include_positions=True"):
        SchematicPlanRequest.from_readback(data)


def test_plan_applies_existing_editor_operations() -> None:
    plan = SchematicPlanner().plan(_acceptance_request())

    class Editor:
        def __init__(self) -> None:
            self.commands: list[str] = []
            self.mos_labels: list[tuple[str, dict[str, str | None]]] = []

        def add(self, command: str) -> None:
            self.commands.append(command)

        def add_net_label_to_transistor(self, name: str, **nets: str | None) -> None:
            self.mos_labels.append((name, nets))

    editor = Editor()
    plan.apply(editor)

    assert any('dbCreateInst(cv rbMaster "M_INP"' in command for command in editor.commands)
    assert any('schCreatePin(cv ' in command and '"VINP"' in command for command in editor.commands)
    assert ("M_INP", {
        "drain_net": "VOP",
        "gate_net": "VINP",
        "source_net": "VS",
        "body_net": "VSS",
    }) in editor.mos_labels


def test_schematic_ops_create_from_plan_executes_schcheck_and_save() -> None:
    class Client:
        def __init__(self) -> None:
            self.operations: list[str] | None = None

        def execute_operations(self, operations: list[str], timeout: int = 60):
            self.operations = operations
            return {"ok": True, "result": {"status": "success", "errors": []}}

    client = Client()
    plan = SchematicPlanner().plan(
        SchematicPlanRequest(
            instances=[SchematicInstanceSpec("M0", "pdk", "nch")],
        )
    )

    SchematicOps(client).create_from_plan("LIB", "CELL", plan)

    assert client.operations is not None
    assert client.operations[-2] == "schCheck(cv)"
    assert "dbSave(rbCv)" in client.operations[-1]


def test_readback_report_is_structured_and_can_be_required() -> None:
    plan = SchematicPlanner().plan(
        SchematicPlanRequest(
            instances=[
                SchematicInstanceSpec(
                    "M0", "pdk", "nch", terminals={"D": "OUT", "G": "IN"}
                )
            ],
            positions=[GridPositionConstraint("M0", col=2, row=1)],
            pins=[SchematicPinSpec("IN", "input", row=1)],
        )
    )
    readback = {
        "instances": [
            {
                "name": "M0",
                "lib": "pdk",
                "cell": "nch",
                "xy": [99, 1.5],
                "orient": "R0",
                "terms": {"D": "WRONG", "G": "IN"},
            }
        ],
        "pins": {},
    }

    report = plan.verify_readback(readback)

    assert not report.ok
    assert {m.code for m in report.mismatches} >= {
        "position_mismatch",
        "terminal_net_mismatch",
        "missing_pin",
    }
    with pytest.raises(AssertionError, match="position_mismatch"):
        report.require_valid()


def test_readback_report_handles_malformed_position_as_a_mismatch() -> None:
    plan = SchematicPlanner().plan(
        SchematicPlanRequest(
            instances=[SchematicInstanceSpec("M0", "pdk", "nch")]
        )
    )
    readback = {
        "instances": [
            {
                "name": "M0",
                "lib": "pdk",
                "cell": "nch",
                "xy": ["not-a-number", 0],
                "orient": "R0",
                "terms": {},
            }
        ],
        "pins": {},
    }

    report = plan.verify_readback(readback)

    assert [m.code for m in report.mismatches] == ["position_mismatch"]


@pytest.mark.parametrize("grid_spacing", [0, -1])
def test_planner_rejects_non_positive_grid_spacing(grid_spacing: float) -> None:
    with pytest.raises(ValueError, match="grid_spacing must be positive"):
        SchematicPlannerConfig(grid_spacing=grid_spacing)

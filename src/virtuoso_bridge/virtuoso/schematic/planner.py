"""Deterministic, constraint-aware planning for generated schematics.

The planner is deliberately a pure Python model layer.  It does not synthesize
topology, route wires, or define a second SKILL language.  A completed
``SchematicPlan`` only feeds the existing schematic editor operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Iterable, Mapping, Sequence

from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_create_inst_by_master_name,
    schematic_create_pin,
    schematic_label_instance_term,
)


class ConstraintStrength(str, Enum):
    """Whether a placement constraint may be relaxed."""

    HARD = "hard"
    SOFT = "soft"


class ConstraintLevel(str, Enum):
    """Outcome represented by a planning diagnostic."""

    RELAXED = "relaxed"
    CONFLICT = "conflict"


class DeviceKind(str, Enum):
    """Small device classification used by the row-placement rule."""

    NMOS = "nmos"
    PMOS = "pmos"
    OTHER = "other"


def _nonempty(value: Any, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _finite(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


def _normalize_strength(value: ConstraintStrength | str) -> ConstraintStrength:
    try:
        return ConstraintStrength(value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in ConstraintStrength)
        raise ValueError(f"strength must be one of: {choices}") from exc


def infer_device_kind(cell: str) -> DeviceKind:
    """Infer MOS polarity from a master-cell name, otherwise return OTHER."""

    normalized = cell.casefold()
    if "pmos" in normalized or "pch" in normalized:
        return DeviceKind.PMOS
    if "nmos" in normalized or "nch" in normalized:
        return DeviceKind.NMOS
    return DeviceKind.OTHER


@dataclass(frozen=True)
class SchematicInstanceSpec:
    """A master, instance name, and explicit terminal connectivity."""

    name: str
    lib: str
    cell: str
    terminals: Mapping[str, str] = field(default_factory=dict)
    view: str = "symbol"
    kind: DeviceKind | str | None = None
    orientation: str | None = None
    output_stage: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "instance name"))
        object.__setattr__(self, "lib", _nonempty(self.lib, "instance lib"))
        object.__setattr__(self, "cell", _nonempty(self.cell, "instance cell"))
        object.__setattr__(self, "view", _nonempty(self.view, "instance view"))
        if self.orientation is not None:
            object.__setattr__(
                self, "orientation", _nonempty(self.orientation, "instance orientation")
            )
        if self.kind is None:
            object.__setattr__(self, "kind", infer_device_kind(self.cell))
        else:
            try:
                object.__setattr__(self, "kind", DeviceKind(self.kind))
            except ValueError as exc:
                choices = ", ".join(item.value for item in DeviceKind)
                raise ValueError(f"kind must be one of: {choices}") from exc
        normalized_terms: dict[str, str] = {}
        for term, net in self.terminals.items():
            normalized_term = _nonempty(term, "terminal name")
            if self.kind in {DeviceKind.NMOS, DeviceKind.PMOS} and normalized_term.upper() in {
                "D",
                "G",
                "S",
                "B",
            }:
                normalized_term = normalized_term.upper()
            normalized_terms[normalized_term] = _nonempty(net, "terminal net")
        object.__setattr__(self, "terminals", normalized_terms)


@dataclass(frozen=True)
class SchematicPinSpec:
    """A top-level pin placed in the planner's dedicated pin column."""

    name: str
    direction: str = "inputOutput"
    row: float | None = None
    orientation: str = "R0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "pin name"))
        object.__setattr__(self, "direction", _nonempty(self.direction, "pin direction"))
        object.__setattr__(
            self, "orientation", _nonempty(self.orientation, "pin orientation")
        )
        if self.row is not None:
            object.__setattr__(self, "row", _finite(self.row, "pin row"))


@dataclass(frozen=True)
class GridPositionConstraint:
    """An exact grid coordinate for one instance.

    At least one of ``col`` and ``row`` must be present.  Hard coordinates are
    never moved; soft coordinates may be displaced, with a diagnostic.
    """

    instance: str
    col: float | None = None
    row: float | None = None
    strength: ConstraintStrength | str = ConstraintStrength.HARD

    def __post_init__(self) -> None:
        object.__setattr__(self, "instance", _nonempty(self.instance, "constraint instance"))
        if self.col is None and self.row is None:
            raise ValueError("a grid position constraint needs col and/or row")
        if self.col is not None:
            object.__setattr__(self, "col", _finite(self.col, "constraint col"))
        if self.row is not None:
            object.__setattr__(self, "row", _finite(self.row, "constraint row"))
        object.__setattr__(self, "strength", _normalize_strength(self.strength))


@dataclass(frozen=True)
class DifferentialPairConstraint:
    """Place two devices symmetrically on one row."""

    left: str
    right: str
    row: float | None = None
    center_col: float = 1.5
    separation: float = 1.0
    strength: ConstraintStrength | str = ConstraintStrength.HARD

    def __post_init__(self) -> None:
        object.__setattr__(self, "left", _nonempty(self.left, "left instance"))
        object.__setattr__(self, "right", _nonempty(self.right, "right instance"))
        if self.left == self.right:
            raise ValueError("differential-pair instances must be different")
        if self.row is not None:
            object.__setattr__(self, "row", _finite(self.row, "pair row"))
        object.__setattr__(self, "center_col", _finite(self.center_col, "pair center_col"))
        separation = _finite(self.separation, "pair separation")
        if separation <= 0:
            raise ValueError("pair separation must be positive")
        object.__setattr__(self, "separation", separation)
        object.__setattr__(self, "strength", _normalize_strength(self.strength))


@dataclass(frozen=True)
class SchematicPlannerConfig:
    """The intentionally small built-in rule set for the MVP planner."""

    grid_spacing: float = 1.5
    nmos_row: float = 0.0
    other_row: float = 1.0
    pmos_row: float = 2.0
    pin_column: float = -1.0
    output_column: float = 5.0
    output_column_step: float = 1.0

    def __post_init__(self) -> None:
        spacing = _finite(self.grid_spacing, "grid_spacing")
        if spacing <= 0:
            raise ValueError("grid_spacing must be positive")
        object.__setattr__(self, "grid_spacing", spacing)
        for field_name in (
            "nmos_row",
            "other_row",
            "pmos_row",
            "pin_column",
            "output_column",
            "output_column_step",
        ):
            object.__setattr__(self, field_name, _finite(getattr(self, field_name), field_name))
        if self.output_column_step <= 0:
            raise ValueError("output_column_step must be positive")


@dataclass(frozen=True)
class ConstraintDiagnostic:
    """A conflict or an observable relaxation of a soft constraint."""

    level: ConstraintLevel
    code: str
    message: str
    subjects: tuple[str, ...] = ()


class SchematicPlanningError(ValueError):
    """Raised when no plan can satisfy all hard constraints."""

    def __init__(self, diagnostics: Sequence[ConstraintDiagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        messages = "; ".join(
            item.message
            for item in self.diagnostics
            if item.level is ConstraintLevel.CONFLICT
        )
        super().__init__(f"schematic planning failed: {messages}")


@dataclass(frozen=True)
class SchematicPlanRequest:
    """Explicit topology metadata plus domain-specific placement constraints."""

    instances: Sequence[SchematicInstanceSpec]
    pins: Sequence[SchematicPinSpec] = ()
    positions: Sequence[GridPositionConstraint] = ()
    differential_pairs: Sequence[DifferentialPairConstraint] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "instances", tuple(self.instances))
        object.__setattr__(self, "pins", tuple(self.pins))
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "differential_pairs", tuple(self.differential_pairs))

    @classmethod
    def from_readback(
        cls,
        schematic: Mapping[str, Any],
        *,
        grid_spacing: float = 1.5,
        strength: ConstraintStrength | str = ConstraintStrength.HARD,
        differential_pairs: Sequence[DifferentialPairConstraint] = (),
        output_instances: Iterable[str] = (),
    ) -> SchematicPlanRequest:
        """Build a recreation request from ``schematic.read`` output.

        The source must have been read with ``include_positions=True``.  Pin
        coordinates are not part of unified readback, so pins are deterministically
        assigned rows in name order and placed in the configured pin column.
        """

        spacing = _finite(grid_spacing, "grid_spacing")
        if spacing <= 0:
            raise ValueError("grid_spacing must be positive")
        normalized_strength = _normalize_strength(strength)
        output_names = {str(name) for name in output_instances}
        instances: list[SchematicInstanceSpec] = []
        positions: list[GridPositionConstraint] = []
        for raw in sorted(
            schematic.get("instances", ()),
            key=lambda item: str(item.get("name", "")),
        ):
            name = _nonempty(raw.get("name", ""), "readback instance name")
            xy = raw.get("xy")
            if not isinstance(xy, (list, tuple)) or len(xy) != 2:
                raise ValueError(
                    f"instance {name!r} has no position; read with include_positions=True"
                )
            orientation = raw.get("orient") or "R0"
            instances.append(
                SchematicInstanceSpec(
                    name=name,
                    lib=raw.get("lib", ""),
                    cell=raw.get("cell", ""),
                    view=raw.get("view") or "symbol",
                    terminals=raw.get("terms") or {},
                    orientation=orientation,
                    output_stage=name in output_names,
                )
            )
            positions.append(
                GridPositionConstraint(
                    name,
                    col=_finite(xy[0], f"{name} x") / spacing,
                    row=_finite(xy[1], f"{name} y") / spacing,
                    strength=normalized_strength,
                )
            )

        raw_pins = schematic.get("pins", {}) or {}
        pins = [
            SchematicPinSpec(
                name=str(name),
                direction=str((metadata or {}).get("direction") or "inputOutput"),
                row=float(index),
            )
            for index, (name, metadata) in enumerate(sorted(raw_pins.items()))
        ]
        return cls(
            instances=instances,
            pins=pins,
            positions=positions,
            differential_pairs=differential_pairs,
        )


@dataclass(frozen=True)
class GridPlacement:
    """Resolved grid and absolute coordinates for an instance."""

    col: float
    row: float
    x: float
    y: float
    orientation: str


@dataclass(frozen=True)
class PlannedInstance:
    spec: SchematicInstanceSpec
    placement: GridPlacement


@dataclass(frozen=True)
class PlannedPin:
    name: str
    direction: str
    col: float
    row: float
    x: float
    y: float
    orientation: str


@dataclass(frozen=True)
class ReadbackMismatch:
    code: str
    subject: str
    expected: Any
    actual: Any

    def __str__(self) -> str:
        return (
            f"{self.code}: {self.subject}: expected {self.expected!r}, "
            f"got {self.actual!r}"
        )


@dataclass(frozen=True)
class SchematicReadbackReport:
    mismatches: tuple[ReadbackMismatch, ...]

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def require_valid(self) -> None:
        """Raise an assertion with all structured mismatch details."""

        if self.mismatches:
            raise AssertionError(
                "schematic readback mismatch: "
                + "; ".join(map(str, self.mismatches))
            )


@dataclass(frozen=True)
class SchematicPlan:
    """A deterministic placement that can be applied through SchematicEditor."""

    instances: tuple[PlannedInstance, ...]
    pins: tuple[PlannedPin, ...]
    diagnostics: tuple[ConstraintDiagnostic, ...]
    grid_spacing: float

    def instance(self, name: str) -> PlannedInstance:
        for item in self.instances:
            if item.spec.name == name:
                return item
        raise KeyError(name)

    def apply(self, editor: Any) -> None:
        """Emit existing instance, terminal-label, and pin operations."""

        for item in self.instances:
            spec = item.spec
            placement = item.placement
            editor.add(
                schematic_create_inst_by_master_name(
                    spec.lib,
                    spec.cell,
                    spec.view,
                    spec.name,
                    placement.x,
                    placement.y,
                    placement.orientation,
                )
            )
            normalized_terms = {str(term).upper(): net for term, net in spec.terminals.items()}
            if spec.kind in {DeviceKind.NMOS, DeviceKind.PMOS}:
                mos_nets = {
                    "drain_net": normalized_terms.get("D"),
                    "gate_net": normalized_terms.get("G"),
                    "source_net": normalized_terms.get("S"),
                    "body_net": normalized_terms.get("B"),
                }
                if any(mos_nets.values()):
                    editor.add_net_label_to_transistor(spec.name, **mos_nets)
                handled = {"D", "G", "S", "B"}
            else:
                handled = set()
            for term, net in sorted(spec.terminals.items()):
                if str(term).upper() not in handled:
                    editor.add(schematic_label_instance_term(spec.name, str(term), str(net)))

        for pin in self.pins:
            editor.add(
                schematic_create_pin(
                    pin.name,
                    pin.x,
                    pin.y,
                    pin.orientation,
                    direction=pin.direction,
                )
            )

    def verify_readback(
        self,
        schematic: Mapping[str, Any],
        *,
        tolerance: float = 1e-6,
    ) -> SchematicReadbackReport:
        """Compare a plan with ``schematic.read(..., include_positions=True)``."""

        if tolerance < 0:
            raise ValueError("tolerance must not be negative")
        mismatches: list[ReadbackMismatch] = []
        actual_instances = {
            str(item.get("name", "")): item for item in schematic.get("instances", ())
        }
        expected_names = {item.spec.name for item in self.instances}
        for item in self.instances:
            spec = item.spec
            actual = actual_instances.get(spec.name)
            if actual is None:
                mismatches.append(ReadbackMismatch("missing_instance", spec.name, "present", None))
                continue
            for field_name, expected in (("lib", spec.lib), ("cell", spec.cell)):
                observed = actual.get(field_name)
                if observed != expected:
                    mismatches.append(
                        ReadbackMismatch(
                            f"{field_name}_mismatch", spec.name, expected, observed
                        )
                    )
            xy = actual.get("xy")
            expected_xy = (item.placement.x, item.placement.y)
            try:
                position_matches = (
                    isinstance(xy, (list, tuple))
                    and len(xy) == 2
                    and math.isclose(float(xy[0]), expected_xy[0], abs_tol=tolerance)
                    and math.isclose(float(xy[1]), expected_xy[1], abs_tol=tolerance)
                )
            except (TypeError, ValueError):
                position_matches = False
            if not position_matches:
                mismatches.append(
                    ReadbackMismatch("position_mismatch", spec.name, expected_xy, xy)
                )
            observed_orientation = actual.get("orient")
            if observed_orientation != item.placement.orientation:
                mismatches.append(
                    ReadbackMismatch(
                        "orientation_mismatch",
                        spec.name,
                        item.placement.orientation,
                        observed_orientation,
                    )
                )
            observed_terms = actual.get("terms") or {}
            for term, expected_net in sorted(spec.terminals.items()):
                observed_net = observed_terms.get(term)
                if observed_net != expected_net:
                    mismatches.append(
                        ReadbackMismatch(
                            "terminal_net_mismatch",
                            f"{spec.name}.{term}",
                            expected_net,
                            observed_net,
                        )
                    )
        for unexpected in sorted(set(actual_instances) - expected_names):
            mismatches.append(
                ReadbackMismatch("unexpected_instance", unexpected, None, "present")
            )

        actual_pins = schematic.get("pins", {}) or {}
        expected_pins = {pin.name: pin for pin in self.pins}
        for name, pin in expected_pins.items():
            observed = actual_pins.get(name)
            if observed is None:
                mismatches.append(ReadbackMismatch("missing_pin", name, "present", None))
            elif observed.get("direction") != pin.direction:
                mismatches.append(
                    ReadbackMismatch(
                        "pin_direction_mismatch",
                        name,
                        pin.direction,
                        observed.get("direction"),
                    )
                )
        for unexpected in sorted(set(actual_pins) - set(expected_pins)):
            mismatches.append(ReadbackMismatch("unexpected_pin", unexpected, None, "present"))
        return SchematicReadbackReport(tuple(mismatches))


@dataclass
class _Assignment:
    value: Any
    strength: ConstraintStrength
    priority: int
    rule: str


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (float, int)) and isinstance(right, (float, int)):
        return math.isclose(float(left), float(right), abs_tol=1e-12)
    return left == right


class SchematicPlanner:
    """Resolve the fixed MVP rule set into a deterministic plan."""

    def __init__(self, config: SchematicPlannerConfig | None = None) -> None:
        self.config = config or SchematicPlannerConfig()

    def plan(self, request: SchematicPlanRequest) -> SchematicPlan:
        diagnostics: list[ConstraintDiagnostic] = []
        specs: dict[str, SchematicInstanceSpec] = {}
        for spec in request.instances:
            if spec.name in specs:
                diagnostics.append(
                    ConstraintDiagnostic(
                        ConstraintLevel.CONFLICT,
                        "duplicate_instance",
                        f"instance name {spec.name!r} is duplicated",
                        (spec.name,),
                    )
                )
            specs[spec.name] = spec
        pins_seen: set[str] = set()
        for pin in request.pins:
            if pin.name in pins_seen:
                diagnostics.append(
                    ConstraintDiagnostic(
                        ConstraintLevel.CONFLICT,
                        "duplicate_pin",
                        f"pin name {pin.name!r} is duplicated",
                        (pin.name,),
                    )
                )
            pins_seen.add(pin.name)

        positions = sorted(
            request.positions,
            key=lambda item: (
                item.instance,
                item.col is None,
                item.col or 0.0,
                item.row is None,
                item.row or 0.0,
                item.strength.value,
            ),
        )
        pairs = sorted(
            request.differential_pairs,
            key=lambda item: (item.left, item.right, item.center_col, item.separation),
        )
        for constraint in positions:
            if constraint.instance not in specs:
                diagnostics.append(
                    ConstraintDiagnostic(
                        ConstraintLevel.CONFLICT,
                        "unknown_instance",
                        f"position constraint references unknown instance {constraint.instance!r}",
                        (constraint.instance,),
                    )
                )
        for pair in pairs:
            missing = tuple(name for name in (pair.left, pair.right) if name not in specs)
            if missing:
                diagnostics.append(
                    ConstraintDiagnostic(
                        ConstraintLevel.CONFLICT,
                        "unknown_pair_instance",
                        "differential pair references unknown instance(s): " + ", ".join(missing),
                        missing,
                    )
                )
        if any(item.level is ConstraintLevel.CONFLICT for item in diagnostics):
            raise SchematicPlanningError(diagnostics)

        assignments: dict[str, dict[str, _Assignment]] = {
            name: {} for name in sorted(specs)
        }

        def assign(
            name: str,
            axis: str,
            value: Any,
            strength: ConstraintStrength,
            priority: int,
            rule: str,
        ) -> None:
            current = assignments[name].get(axis)
            proposed = _Assignment(value, strength, priority, rule)
            if current is None:
                assignments[name][axis] = proposed
                return
            if _same_value(current.value, value):
                if (
                    strength is ConstraintStrength.HARD
                    and current.strength is ConstraintStrength.SOFT
                ):
                    assignments[name][axis] = proposed
                elif strength is current.strength and priority > current.priority:
                    assignments[name][axis] = proposed
                return
            subject = f"{name}.{axis}"
            if current.strength is ConstraintStrength.HARD and strength is ConstraintStrength.HARD:
                diagnostics.append(
                    ConstraintDiagnostic(
                        ConstraintLevel.CONFLICT,
                        "hard_constraint_conflict",
                        f"{subject} is {current.value!r} from {current.rule}, "
                        f"but {rule} requires {value!r}",
                        (subject,),
                    )
                )
                return
            new_wins = (
                strength is ConstraintStrength.HARD
                or (
                    current.strength is ConstraintStrength.SOFT
                    and strength is ConstraintStrength.SOFT
                    and priority > current.priority
                )
            )
            winner = proposed if new_wins else current
            loser = current if new_wins else proposed
            assignments[name][axis] = winner
            diagnostics.append(
                ConstraintDiagnostic(
                    ConstraintLevel.RELAXED,
                    "soft_constraint_relaxed",
                    f"{subject}: relaxed {loser.rule}={loser.value!r}; "
                    f"using {winner.rule}={winner.value!r}",
                    (subject,),
                )
            )

        constrained_columns = {
            constraint.instance for constraint in positions if constraint.col is not None
        }
        pair_members = {name for pair in pairs for name in (pair.left, pair.right)}
        core_index = 0
        output_index = 0
        for name, spec in sorted(specs.items()):
            row = {
                DeviceKind.NMOS: self.config.nmos_row,
                DeviceKind.PMOS: self.config.pmos_row,
                DeviceKind.OTHER: self.config.other_row,
            }[spec.kind]
            assign(name, "row", row, ConstraintStrength.SOFT, 10, "polarity-row")
            if spec.output_stage:
                assign(
                    name,
                    "col",
                    self.config.output_column + output_index * self.config.output_column_step,
                    ConstraintStrength.SOFT,
                    30,
                    "output-stage-column",
                )
                output_index += 1
            elif name not in pair_members and name not in constrained_columns:
                assign(
                    name,
                    "col",
                    float(core_index),
                    ConstraintStrength.SOFT,
                    10,
                    "deterministic-grid-order",
                )
                core_index += 1
            if spec.orientation is not None:
                assign(
                    name,
                    "orientation",
                    spec.orientation,
                    ConstraintStrength.HARD,
                    100,
                    "explicit-orientation",
                )

        for constraint in positions:
            if constraint.instance not in specs:
                continue
            rule = f"explicit-position({constraint.instance})"
            if constraint.col is not None:
                assign(
                    constraint.instance,
                    "col",
                    constraint.col,
                    constraint.strength,
                    100,
                    rule,
                )
            if constraint.row is not None:
                assign(
                    constraint.instance,
                    "row",
                    constraint.row,
                    constraint.strength,
                    100,
                    rule,
                )

        for pair in pairs:
            if pair.left not in specs or pair.right not in specs:
                continue
            if pair.row is not None:
                pair_row = pair.row
            else:
                left_row = assignments[pair.left]["row"]
                right_row = assignments[pair.right]["row"]
                hard_rows = [
                    item.value
                    for item in (left_row, right_row)
                    if item.strength is ConstraintStrength.HARD
                ]
                pair_row = hard_rows[0] if hard_rows else left_row.value
            half_separation = pair.separation / 2.0
            pair_rule = f"differential-pair({pair.left},{pair.right})"
            assign(
                pair.left,
                "col",
                pair.center_col - half_separation,
                pair.strength,
                50,
                pair_rule,
            )
            assign(
                pair.right,
                "col",
                pair.center_col + half_separation,
                pair.strength,
                50,
                pair_rule,
            )
            assign(pair.left, "row", pair_row, pair.strength, 50, pair_rule)
            assign(pair.right, "row", pair_row, pair.strength, 50, pair_rule)
            assign(pair.left, "orientation", "R0", pair.strength, 50, pair_rule)
            assign(pair.right, "orientation", "MY", pair.strength, 50, pair_rule)

        for name in sorted(specs):
            assign(
                name,
                "orientation",
                "R0",
                ConstraintStrength.SOFT,
                0,
                "default-orientation",
            )
            if "col" not in assignments[name]:
                assign(
                    name,
                    "col",
                    float(core_index),
                    ConstraintStrength.SOFT,
                    0,
                    "fallback-grid-order",
                )
                core_index += 1

        self._resolve_collisions(assignments, diagnostics)
        self._check_pair_results(pairs, assignments, diagnostics)
        if any(item.level is ConstraintLevel.CONFLICT for item in diagnostics):
            raise SchematicPlanningError(diagnostics)

        spacing = self.config.grid_spacing
        planned_instances = tuple(
            PlannedInstance(
                specs[name],
                GridPlacement(
                    col=float(assignments[name]["col"].value),
                    row=float(assignments[name]["row"].value),
                    x=float(assignments[name]["col"].value) * spacing,
                    y=float(assignments[name]["row"].value) * spacing,
                    orientation=str(assignments[name]["orientation"].value),
                ),
            )
            for name in sorted(specs)
        )
        planned_pins = tuple(
            PlannedPin(
                name=pin.name,
                direction=pin.direction,
                col=self.config.pin_column,
                row=pin.row if pin.row is not None else float(index),
                x=self.config.pin_column * spacing,
                y=(pin.row if pin.row is not None else float(index)) * spacing,
                orientation=pin.orientation,
            )
            for index, pin in enumerate(sorted(request.pins, key=lambda item: item.name))
        )
        return SchematicPlan(
            instances=planned_instances,
            pins=planned_pins,
            diagnostics=tuple(diagnostics),
            grid_spacing=spacing,
        )

    @staticmethod
    def _resolve_collisions(
        assignments: dict[str, dict[str, _Assignment]],
        diagnostics: list[ConstraintDiagnostic],
    ) -> None:
        while True:
            locations: dict[tuple[float, float], list[str]] = {}
            for name, axes in assignments.items():
                location = (float(axes["col"].value), float(axes["row"].value))
                locations.setdefault(location, []).append(name)
            collisions = [
                (location, sorted(names))
                for location, names in sorted(locations.items())
                if len(names) > 1
            ]
            if not collisions:
                return
            location, names = collisions[0]
            movable = [
                name
                for name in names
                if assignments[name]["col"].strength is ConstraintStrength.SOFT
                or assignments[name]["row"].strength is ConstraintStrength.SOFT
            ]
            if not movable:
                diagnostics.append(
                    ConstraintDiagnostic(
                        ConstraintLevel.CONFLICT,
                        "hard_grid_collision",
                        f"hard-constrained instances {', '.join(names)} "
                        f"occupy grid cell {location}",
                        tuple(names),
                    )
                )
                return
            hard_axis_counts = {
                name: sum(
                    assignments[name][axis].strength is ConstraintStrength.HARD
                    for axis in ("col", "row")
                )
                for name in movable
            }
            fewest_hard_axes = min(hard_axis_counts.values())
            mover = sorted(
                name for name in movable if hard_axis_counts[name] == fewest_hard_axes
            )[-1]
            axes = assignments[mover]
            occupied = set(locations)
            old_location = (float(axes["col"].value), float(axes["row"].value))
            if axes["col"].strength is ConstraintStrength.SOFT:
                new_col = float(axes["col"].value) + 1.0
                while (new_col, float(axes["row"].value)) in occupied:
                    new_col += 1.0
                axes["col"] = _Assignment(
                    new_col,
                    ConstraintStrength.SOFT,
                    axes["col"].priority,
                    "collision-resolution",
                )
            else:
                new_row = float(axes["row"].value) + 1.0
                while (float(axes["col"].value), new_row) in occupied:
                    new_row += 1.0
                axes["row"] = _Assignment(
                    new_row,
                    ConstraintStrength.SOFT,
                    axes["row"].priority,
                    "collision-resolution",
                )
            new_location = (float(axes["col"].value), float(axes["row"].value))
            diagnostics.append(
                ConstraintDiagnostic(
                    ConstraintLevel.RELAXED,
                    "soft_grid_collision_resolved",
                    f"moved {mover} from {old_location} to {new_location} to avoid overlap",
                    (mover,),
                )
            )

    @staticmethod
    def _check_pair_results(
        pairs: Sequence[DifferentialPairConstraint],
        assignments: Mapping[str, Mapping[str, _Assignment]],
        diagnostics: list[ConstraintDiagnostic],
    ) -> None:
        for pair in pairs:
            if pair.left not in assignments or pair.right not in assignments:
                continue
            left = assignments[pair.left]
            right = assignments[pair.right]
            expected_left = pair.center_col - pair.separation / 2.0
            expected_right = pair.center_col + pair.separation / 2.0
            valid = (
                _same_value(left["col"].value, expected_left)
                and _same_value(right["col"].value, expected_right)
                and _same_value(left["row"].value, right["row"].value)
                and left["orientation"].value == "R0"
                and right["orientation"].value == "MY"
            )
            if valid:
                continue
            level = (
                ConstraintLevel.CONFLICT
                if pair.strength is ConstraintStrength.HARD
                else ConstraintLevel.RELAXED
            )
            code = (
                "hard_differential_pair_conflict"
                if level is ConstraintLevel.CONFLICT
                else "soft_differential_pair_relaxed"
            )
            diagnostics.append(
                ConstraintDiagnostic(
                    level,
                    code,
                    f"differential-pair rule for {pair.left}/{pair.right} is not fully satisfied",
                    (pair.left, pair.right),
                )
            )


__all__ = [
    "ConstraintDiagnostic",
    "ConstraintLevel",
    "ConstraintStrength",
    "DeviceKind",
    "DifferentialPairConstraint",
    "GridPlacement",
    "GridPositionConstraint",
    "PlannedInstance",
    "PlannedPin",
    "ReadbackMismatch",
    "SchematicInstanceSpec",
    "SchematicPinSpec",
    "SchematicPlan",
    "SchematicPlanRequest",
    "SchematicPlanner",
    "SchematicPlannerConfig",
    "SchematicPlanningError",
    "SchematicReadbackReport",
    "infer_device_kind",
]

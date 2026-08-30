# ADR 0002: Deterministic Python-side schematic planning

- Status: Accepted
- Date: 2026-08-30
- Related: Issue #142

## Context

The schematic API already has reliable execution primitives for placing an
instance, labeling device terminals, creating pins, running `schCheck`, and
saving a cellview.  Schematic recreation guidance also describes useful
placement conventions: a 1.5-unit grid, NMOS/PMOS row separation, mirrored
differential pairs, a dedicated pin column, and output stages shifted right.

Until now, callers had to translate those conventions into coordinates by
hand.  A general schematic synthesis engine or user-defined constraint
language would create a second abstraction over SKILL, obscure electrical
intent, and substantially widen the project's scope.

## Decision

Add an optional, pure-Python planning layer in
`virtuoso_bridge.virtuoso.schematic.planner`.  It accepts explicit device and
connectivity metadata and emits a deterministic `SchematicPlan`.  Applying the
plan reuses the existing `SchematicEditor` and operation builders.

The public input model is:

- `SchematicInstanceSpec`: master, name, terminal-to-net map, optional explicit
  device kind/orientation, and an output-stage marker;
- `SchematicPinSpec`: name, direction, optional grid row, and orientation;
- `GridPositionConstraint`: an instance column and/or row with hard or soft
  strength;
- `DifferentialPairConstraint`: ordered left/right devices, center column,
  separation, optional row, and hard or soft strength;
- `SchematicPlannerConfig`: grid spacing and the fixed NMOS, PMOS, other, pin,
  and output-stage defaults.

`SchematicPlanRequest.from_readback()` consumes the result of
`schematic.read(..., include_positions=True)` for deterministic recreation.
It preserves instance placement, orientation, master, and connectivity.  The
unified reader does not expose pin coordinates, so readback pins are assigned
rows in name order and placed in the dedicated pin column.

### Hard and soft semantics

Hard constraints are invariants.  Two incompatible hard assignments, an
unknown referenced instance, a duplicate name, or an unmovable grid collision
raises `SchematicPlanningError` before any Virtuoso operation is sent.

Soft rules are preferences.  The planner may relax one to satisfy a stronger
rule or avoid an overlap, but every relaxation is recorded as a structured
`ConstraintDiagnostic`.  This makes fallback visible to callers and tests.

The planner's built-in rules are intentionally finite:

1. put NMOS, other devices, and PMOS on configured rows;
2. place ordered differential pairs on one row with `R0`/`MY` symmetry;
3. put top-level pins in one configured column;
4. shift devices marked `output_stage=True` to configured output columns;
5. move soft placements deterministically to avoid grid-cell collisions.

Input order never determines the result: instances, pins, pairs, and
constraints are normalized by stable names and coordinates.

### Execution and verification

`client.schematic.create_from_plan(lib, cell, plan)` deliberately replaces the
target schematic, applies the plan, then follows the existing editor exit path
that runs `schCheck` and saves.  There is no separate SKILL DSL.

After creation, callers can read the schematic back and verify it:

```python
data = client.schematic.read(lib, cell, include_positions=True)
report = plan.verify_readback(data)
report.require_valid()
```

The report checks instance presence, masters, positions, orientations,
terminal connectivity, and top-level pin presence/direction.

## Acceptance example

The executable example
`examples/01_virtuoso/schematic/12_plan_differential_pair.py` plans a
differential pair with a tail device, PMOS load pair, output stage, and pins.
It creates the cellview, relies on the editor's `schCheck`, reads the result
back, and requires an exact plan match.  Unit tests run the same topology
without needing a Cadence installation and verify that reversed input order
produces the same plan.

## Non-goals

- topology or device synthesis;
- global routing or wire optimization;
- arbitrary expressions or a generic rule language;
- inferring differential pairs or output stages from net names;
- silently repairing incompatible electrical intent.

These remain explicit caller responsibilities.  Future rule types should be
added only for recurring schematic-domain concepts with deterministic conflict
semantics.

## Consequences

Callers gain a testable planning boundary and useful placement defaults without
changing the proven SKILL execution layer.  The model is intentionally less
powerful than general constraint solvers; designs outside its rule set can
still emit existing schematic operations directly.

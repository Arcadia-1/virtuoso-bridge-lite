# Schematic Python API

Python wrapper for Cadence Virtuoso schematic editing via SKILL.

**Package:** `virtuoso_bridge.virtuoso.schematic`

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
# SchematicOps is accessed via client.schematic
```

## SchematicEditor (context manager)

Collects SKILL commands, executes as a batch on `__exit__`, then runs `schCheck` + `dbSave` automatically.

```python
from virtuoso_bridge.virtuoso.schematic import (
    schematic_create_inst_by_master_name as inst,
    schematic_create_pin as pin,
    schematic_create_wire_between_instance_terms as wire,
)

with client.schematic.create(lib, cell) as sch:
    sch.add(inst("analogLib", "vdc", "symbol", "V0", 0, 0, "R0"))
    sch.add(wire("V0", "PLUS", "R0", "PLUS"))
    sch.add(pin("OUT", 3.0, 0.5, "R0", direction="output"))
    sch.add_net_label_to_transistor("M0", drain_net="OUT", gate_net="IN",
        source_net="VSS", body_net="VSS")
    # schCheck + dbSave happen automatically on exit
```

### SchematicEditor methods

| Method | Description |
|--------|-------------|
| `add(skill_cmd)` | Queue any SKILL command string (from ops functions) |
| `add_net_label_to_transistor(inst, drain_net, gate_net, source_net, body_net)` | Label MOS D/G/S/B terminals with net stubs |

### Open mode is explicit

Use `client.schematic.create(lib, cell)` to create or deliberately replace a
schematic. Use `client.schematic.modify(lib, cell)` to append changes to an
existing schematic without clearing it. The legacy `edit()` method is
deprecated and now defaults to safe append mode.

Read a schematic through the same client-bound API:

```python
data = client.schematic.read(lib, cell, include_positions=False)
```

## Deterministic constraint planning

Use the optional Python-side planner when placement conventions should be
repeatable and conflicts must be detected before editing Virtuoso. It keeps
connectivity explicit and reuses the existing `SchematicEditor`; it does not
synthesize topology or route wires.

```python
from virtuoso_bridge.virtuoso.schematic import (
    DeviceKind,
    DifferentialPairConstraint,
    GridPositionConstraint,
    SchematicInstanceSpec,
    SchematicPinSpec,
    SchematicPlanRequest,
)

request = SchematicPlanRequest(
    instances=[
        SchematicInstanceSpec("M_TAIL", PDK, NCH, kind=DeviceKind.NMOS,
            terminals={"D": "VS", "G": "BIAS", "S": "VSS", "B": "VSS"}),
        SchematicInstanceSpec("M_INP", PDK, NCH, kind=DeviceKind.NMOS,
            terminals={"D": "VOP", "G": "VINP", "S": "VS", "B": "VSS"}),
        SchematicInstanceSpec("M_INN", PDK, NCH, kind=DeviceKind.NMOS,
            terminals={"D": "VON", "G": "VINN", "S": "VS", "B": "VSS"}),
    ],
    pins=[SchematicPinSpec("VINP", "input", row=1)],
    positions=[GridPositionConstraint("M_TAIL", col=1.5, row=0)],
    differential_pairs=[
        DifferentialPairConstraint("M_INP", "M_INN", row=1, center_col=1.5)
    ],
)

plan = client.schematic.plan(request)
client.schematic.create_from_plan(lib, cell, plan)  # includes schCheck + save
report = plan.verify_readback(
    client.schematic.read(lib, cell, include_positions=True)
)
report.require_valid()
```

Built-in rules cover the 1.5-unit grid, NMOS/PMOS rows, `R0`/`MY`
differential-pair symmetry, a dedicated pin column, and output-stage column
offsets. `GridPositionConstraint` and `DifferentialPairConstraint` accept
`strength=ConstraintStrength.HARD` or `.SOFT`:

- incompatible hard constraints raise `SchematicPlanningError` before any edit;
- relaxed soft constraints remain visible in `plan.diagnostics`;
- input ordering does not change the resulting plan.

To recreate an existing design, read positions and convert them to a request:

```python
source = client.schematic.read(src_lib, src_cell, include_positions=True)
request = SchematicPlanRequest.from_readback(source)
plan = client.schematic.plan(request)
client.schematic.create_from_plan(dst_lib, dst_cell, plan)
```

See `examples/01_virtuoso/schematic/12_plan_differential_pair.py` and
`docs/adr/0002-deterministic-schematic-planner.md` for the complete acceptance
example, conflict model, and explicit non-goals.

### SKILL builder functions (ops)

Use these with `sch.add(...)`:

| Function | SKILL | Description |
|----------|-------|-------------|
| `schematic_create_inst_by_master_name(lib, cell, view, name, x, y, orient)` | `dbOpenCellViewByType` + `dbCreateInst` | Place instance |
| `schematic_create_wire(points)` | `schCreateWire` | Add wire from point list |
| `schematic_create_wire_label(x, y, text, just, rot)` | `schCreateWireLabel` | Add wire label |
| `schematic_create_pin(name, x, y, orient, *, direction)` | `schCreatePin` | Add pin |
| `schematic_create_pin_at_instance_term(inst, term, pin, *, direction, orientation)` | `schCreatePin` at terminal center | Pin at terminal |
| `schematic_create_wire_between_instance_terms(from_inst, from_term, to_inst, to_term)` | `schCreateWire` between terminal centers | Wire two terminals |
| `schematic_label_instance_term(inst, term, net)` | Wire stub + label | Label terminal |
| `schematic_create_net_stub(net, x, y, *, direction, length)` | wire + `schCreateWireLabel` | Short named electrical connection |
| `schematic_create_net_expression(net, expression, x, y)` | `schCreateNetExpression` | Attach inherited-connection expression |
| `schematic_set_netset_property(instance, property, net)` | `dbReplaceProp` | Set an inherited-connection override |

## Netlist import and export

`client.schematic.export_netlist()` creates a fresh Virtuoso netlist package,
downloads it to the caller host, and verifies that `input.scs` is present.
`client.schematic.import_netlist()` runs `spiceIn` and converts the imported
netlist view into a schematic. Both paths return structured result objects;
inspect their status and diagnostics instead of assuming a produced directory
or cellview is valid.

```python
exported = client.schematic.export_netlist(
    "demoLib", "tb_amp", "artifacts/tb_amp_netlist", simulator="spectre"
)

imported = client.schematic.import_netlist(
    "demoLib", "imported_amp", "artifacts/amp.scs", language="Spectre",
    overwrite=False,
)
```

For format and `spiceIn` limitations, see [netlist.md](netlist.md).

## Low-level SKILL builders

`schematic/ops.py` — build SKILL strings without executing. Used internally by `SchematicOps` and `SchematicEditor`.

| Function | SKILL generated |
|----------|----------------|
| `schematic_create_inst(master_expr, name, x, y, orient)` | `dbCreateInst(cv master ...)` |
| `schematic_create_inst_by_master_name(lib, cell, view, name, x, y, orient)` | `dbOpenCellViewByType` + `dbCreateInst` |
| `schematic_create_wire(points)` | `schCreateWire(cv "route" "full" ...)` |
| `schematic_create_wire_label(x, y, text, just, rot)` | `schCreateWireLabel(cv ...)` |
| `schematic_create_pin(name, x, y, orient)` | `schCreatePin(cv ...)` |
| `schematic_create_pin_at_instance_term(inst, term, pin)` | Terminal center lookup + `schCreatePin` |
| `schematic_create_wire_between_instance_terms(from_inst, from_term, to_inst, to_term)` | Terminal center lookup + `schCreateWire` |
| `schematic_label_instance_term(inst, term, net)` | Terminal center + MOS-aware stub + `schCreateWireLabel` |
| `schematic_check()` | `schCheck(cv)` |

## Terminal-aware helpers

`add_wire_between_instance_terms` and `add_net_label_to_instance_term` resolve pin positions from the database — no need to guess coordinates.

`add_net_label_to_transistor` is MOS-aware: it knows drain/source go up/down (flipped for PMOS), gate goes left, body goes right. The stub direction adapts to the transistor orientation.

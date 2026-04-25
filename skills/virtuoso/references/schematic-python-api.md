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

with client.schematic.edit(lib, cell) as sch:
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

## SchematicOps (direct execution)

Same operations as `SchematicEditor` but executed immediately (not batched).

```python
client.schematic.add_instance("analogLib", "vdc", (0, 0), name="V0")
client.schematic.add_wire_between_instance_terms("V0", "PLUS", "R0", "PLUS")
```

| Method | SKILL | Description |
|--------|-------|-------------|
| `open(lib, cell, *, view, mode)` | `dbOpenCellViewByType` | Open cellview |
| `save()` | `dbSave(cv)` | Save current cellview |
| `check()` | `schCheck(cv)` | Run schematic check |
| `add_instance(lib, cell, xy, *, orientation, view, name)` | `dbCreateInst` | Add instance |
| `add_wire(points)` | `schCreateWire` | Add wire |
| `add_label(xy, text, *, justification, rotation)` | `schCreateWireLabel` | Add label |
| `add_pin(name, xy, *, orientation, direction)` | `schCreatePin` | Add pin |
| `add_pin_to_instance_term(inst, term, pin_name, *, direction, orientation)` | `schCreatePin` at terminal | Add pin at terminal |
| `add_wire_between_instance_terms(from_inst, from_term, to_inst, to_term)` | `schCreateWire` between terminals | Wire two terminals |
| `add_net_label_to_instance_term(inst, term, net_name)` | Wire stub + label | Label terminal |
| `add_net_label_to_transistor(inst, drain, gate, source, body)` | Multiple wire stubs | Label MOS D/G/S/B |

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

# Recreate a Schematic from an Existing Design

Read an existing schematic, map to a grid, redraw cleanly with stubs. Useful for learning placement from reference designs or generating variants.

## Step 1: Read the original

Extract instances, connectivity, and positions:

```python
from virtuoso_bridge.virtuoso.schematic.reader import read_schematic

data = read_schematic(client, LIB, CELL, include_positions=True)
# data["instances"] has xy, orient, params, terms for each instance
```

## Step 2: Map to grid

Analyze relative positions, assign (col, row) on a uniform grid:

- Sort instances by y -> assign rows (vertical layers)
- Sort by x within each row -> assign columns
- Identify differential pairs (same row, symmetric x) -> left R0, right MY
- Choose GRID spacing (1.5 works well for stub labels without collision)

## Step 3: Redraw

Place on grid with stubs and pins:

```python
GRID = 1.5

# Define placement as (name, cell, col, row, orient)
INSTANCES = [
    ("M_TAIL", "nch_ulvt_mac", 1.5, 0, "R0"),   # centered
    ("M_INP",  "nch_ulvt_mac", 1,   1, "R0"),    # left of pair
    ("M_INN",  "nch_ulvt_mac", 2,   1, "MY"),    # right, mirrored
    ...
]

# Define connectivity as (name, drain, gate, source, body)
LABELS = [
    ("M_TAIL", "VS",  "CLK",  "GND", "GND"),
    ("M_INP",  "VN1", "VINP", "VS",  "GND"),
    ...
]

with client.schematic.edit(LIB, CELL) as sch:
    for name, cell, col, row, orient in INSTANCES:
        sch.add(inst(PDK, cell, "symbol", name, col * GRID, row * GRID, orient))
    for name, d, g, s, b in LABELS:
        sch.add_net_label_to_transistor(name,
            drain_net=d, gate_net=g, source_net=s, body_net=b)
    # Pins in leftmost column
    sch.add(pin("VINP", -1 * GRID, 1 * GRID, "R0", direction="input"))
    ...
```

## Key rules

- **Grid spacing 1.5** -- enough room for stubs without collision. Too small (< 1.0) causes overlap, too large (> 2.0) wastes space.
- **Differential pairs: R0/MY** -- left device `R0`, right device `MY`, same row, symmetric columns.
- **Vertical layering** -- NMOS at bottom (low rows), PMOS at top (high rows). Within a stage: current sources -> signal path -> loads.
- **Pins in a dedicated column** -- always to the left of all transistors (e.g. col = -1).
- **Output stages offset right** -- place at col 5-6, separate from core.
- **No wires** -- only `add_net_label_to_transistor`. Same net name = same net.
- **Verify with CIW screenshot** -- check for PARSER WARNING or schCheck errors after every run.

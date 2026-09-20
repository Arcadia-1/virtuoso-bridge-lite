# Exact-coordinate schematic manifest import

Use this workflow when another editor or generator already knows the intended
placement and wire geometry.  It complements `SchematicPlanner`: the planner
applies a small set of analog placement rules, while manifest import preserves
source coordinates and explicit routes.

## Public API

```python
result = client.schematic.import_manifest(
    "source.json",
    "process-map.json",
    processes=["pdk180", "pdk28"],  # omitted means all
    cells=["ota", "comparator"],    # omitted means all
    verify=True,
    validate_masters=True,
)

pngs = client.schematic.capture_import_result(
    result,
    "output/schematic-evidence",
    margin=0.75,
)
```

The arguments may be paths or already-loaded dictionaries.  For offline
conversion and tests:

```python
from virtuoso_bridge.virtuoso.schematic import plan_manifest_circuit

prepared, layout = plan_manifest_circuit(source_circuit, process_map, "pdk180")
assert layout["geometryAudit"]["afterViolationCount"] == 0
```

## Source contract

The top-level object contains `circuits`.  Each circuit contains:

- `cellName`, `instances`, `ports`, and `nets`;
- `sourceGeometry.bounds`;
- exact `portOccurrences`, `junctions`, `routes`, and `contacts`;
- optional `localBulkLabels`, `localBulkShorts`, and `annotationStubs`.

Each instance has a stable `id` and `reference`, `deviceClass` plus optional
`kind`, `sourcePosition`, `sourceTransform`, terminal `nodes`, and optional
`sourceParameters`.  Route/contact endpoints refer to instance ids, port
occurrence ids, or junction ids and carry their original `sourcePoint`.

Coordinates use source-editor convention: x increases right and y increases
down.  The converter flips y for Virtuoso.  Source rotations are multiples of
90 degrees and mirrors are `none`, `horizontal`, `vertical`, or `both`.

See `examples/01_virtuoso/schematic_manifest/source.example.json` for the
complete JSON shape.

## Process-map contract

Required global fields:

- `gridUnit`: target schematic database/grid unit in user units;
- `sourceScaleInGridUnits`: source coordinate to target-grid scale;
- `originInGridUnits`: target x/y translation;
- `localLabelStubInGridUnits`: local bulk label length; and
- `processes`: named PDK mappings.

Every process has `outputLibrary` and device mappings.  A device mapping has a
target `library`, `cell`, optional `view`, `pinMap`, `pinOffsets`, and optional
`parameterMap` / `parameterOverrides`.  `pinOffsets` are target symbol terminal
centers measured in grid units from the symbol origin.  Import probes the live
master and fails before editing if the coordinates differ.

Never guess a PDK cell name, terminal, offset, or CDF parameter.  Inspect the
live symbol/CDF and keep the resulting map private when the PDK license forbids
redistribution.

## Deterministic behavior

For every horizontal or vertical source edge, the converter creates an exact
coordinate equality between target endpoints.  It solves each connected axis
component with one deterministic translation chosen nearest to the directly
scaled source coordinates.  Inconsistent equalities raise an error.  Diagonal
edges retain their direction and are drawn directly.

Target symbols can have different pin offsets from the source.  Instance
origins therefore move as needed while the source endpoint relations remain
exact.  A deterministic dogleg is added only when that PDK adaptation makes a
wire cross a foreign terminal.

Electrical connectivity does not depend on wire touching.  The workflow first
creates named nets and binds every instance terminal directly, then adds the
visible drawing.  Repeated top-level port names are supported and created as
`inputOutput`, which permits fly-wires and local labels.

After `schCheck` and save, readback verifies instance masters, terminal-to-net
partitions, top-level pins, and mapped CDF parameters.  PNG capture uses the
numeric window returned by `open_window`, so concurrent Virtuoso windows do not
silently produce evidence from the wrong cell.

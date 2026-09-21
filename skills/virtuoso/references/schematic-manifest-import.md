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
    overwrite=False,                 # refuse existing targets by default
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

Preflight is strict: schemas must be the supported v1 identifiers; circuit,
instance, route, contact, port-occurrence, and junction identifiers must be
unique; coordinates must be finite; rotations must be exact multiples of 90°;
and every net, pin, and endpoint reference must resolve unambiguously. Multiple
selections may not resolve to the same output library/cell.

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

No generic MOS dimensions are supplied. Parameters absent from both
`sourceParameters` and `parameterOverrides` remain at the target-master
default. CDF callbacks run under cleanup protection; callback failures abort
staging, and readback rejects values rewritten by the PDK.

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

The workflow creates named nets, binds instance terminals, and draws every
declared route. `schCheck` is authoritative: if Cadence splits disconnected
geometry or reports an error, import fails instead of forcing an OA net merge.
Repeated top-level port names are supported and created as `inputOutput`, which
permits explicit fly-wires and local labels.

The target is never edited in place. Import builds a uniquely named staging
cell, checks/saves/reads it twice, and requires stable topology across both
passes. Readback verifies instance masters, placement, orientation,
terminal-to-net partitions, port-to-net mapping, repeated port counts, and
mapped CDF parameters. Only then is the staging view copied into place. With
`overwrite=True`, the prior target remains in a private backup until the
installed copy passes its final check and readback; any failure restores it.
The former `dbMergeNet`-after-check workaround is intentionally absent because
a subsequent `schCheck` can undo such a merge.

PNG capture uses the numeric window returned by `open_window`, so concurrent
Virtuoso windows do not silently produce evidence from the wrong cell.

# Exact schematic manifest import

This workflow recreates an existing schematic drawing in one or more target
PDKs.  It is for source tools that can export exact instance origins, transforms,
terminal endpoints, wire bends, junctions, and repeated port occurrences.

It does **not** infer topology or placement.  Instead it:

1. maps each source device class to an explicit target symbol master;
2. probes the live target symbols and checks every configured pin offset;
3. solves source horizontal/vertical coordinate equalities exactly;
4. preserves source diagonal segments directly;
5. creates named connectivity and visible wires in Virtuoso;
6. builds a temporary cell and runs two `schCheck` + readback passes;
7. installs the verified cell transactionally, with backup/rollback when
   replacing an existing target; and
8. optionally opens each exact editor window, fits it, and saves a PNG.

## Configure a PDK

Copy `process-map.example.json`, then replace the placeholder output library and
device master.  `pinOffsets` are symbol terminal centers expressed in multiples
of `gridUnit`.  Keep these values under version control: import stops before
editing anything when a live symbol no longer matches them.

The importer does not invent MOS dimensions. Every CDF value must come from
`sourceParameters` or an explicit `parameterOverrides` entry. Readback fails if
a PDK callback clamps or rewrites the requested value, which usually means the
process map selected the wrong public CDF parameter.

Device mappings may be nested under `processes.<name>.devices` or written next
to `outputLibrary`.  `sharedDevices` is useful for `analogLib` passives shared
by every PDK.  Each mapping supports:

- `library`, `cell`, and optional `view`;
- `pinMap` from source terminal name to target terminal name;
- `pinOffsets` for every used target terminal;
- `parameterMap` from source parameter to target CDF parameter; and
- optional `parameterOverrides` for target-PDK legal values.

## Run

Start a healthy bridge profile, then run:

```bash
python examples/01_virtuoso/schematic_manifest/import_and_capture.py \
  --profile my-profile \
  --manifest my-source.json \
  --process-map my-process-map.json
```

Existing targets are refused by default. Add `--overwrite` only when replacement
is intended; the old schematic is backed up until the installed copy passes its
final check and readback.

Select repeatable subsets with `--process PROCESS` and `--cell CELL`.  The
output directory contains a machine-readable import report and one PNG per
generated cell.

The same workflow is available as Python API:

```python
result = client.schematic.import_manifest(
    "my-source.json",
    "my-process-map.json",
    processes=["demo180", "demo28"],
    cells=["ota_5t", "strongarm"],
    overwrite=False,
)
client.schematic.capture_import_result(result, "output/evidence")
```

All generated top-level pin occurrences use `inputOutput`.  Repeating a port
name is intentional and supports local net labels or fly-wires without drawing
one long global wire.  Local bulk labels and local bulk-to-source shorts are
represented independently in `sourceGeometry`.

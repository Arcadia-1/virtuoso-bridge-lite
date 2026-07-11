# Symbol Python API

Python wrappers for Cadence Virtuoso symbol generation, editing, and readback.

**Package:** `virtuoso_bridge.virtuoso.symbol`

```python
from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env()
```

## Generate From Schematic

```python
result = client.symbol.generate_from_schematic(
    "demoLib",
    "nand2",
    schematic_view="schematic",
    symbol_view="symbol",
    sort_pins="geometric",
    overwrite=False,
    timeout=60,
)
```

The helper runs `schSchemToPinList` and `schPinListToSymbol` on a unique
temporary view, validates its terminals and effective pin order, then installs
and verifies the requested symbol view in the same SKILL transaction.

- `sort_pins` accepts `"alphanumeric"`, `"geometric"`, or `None`. A requested
  value temporarily overrides `ssgSortPins`; the previous value is restored
  before the destination is modified. This setting controls physical pin
  placement, not the logical order returned in `pin_order`.
- `overwrite` defaults to `False`. When `True`, the existing target is replaced
  only after temporary-view validation succeeds. An open target is rejected;
  otherwise the helper creates a private backup and restores it if the copy or
  final validation fails.
- `SymbolGenerationResult.action` is `"created"` or `"replaced"`.
- `terminal_names` contains the generated terminal names.
- `pin_order` is the effective order returned by Cadence's
  `schGetPinOrder()`, which resolves an explicit `portOrder` or the native
  default order.

Temporary-view, backup, pin-sort restoration, and rollback failures are
reported instead of being reduced to CIW warnings. If rollback itself fails,
the exception identifies the retained backup view for manual recovery.

## Read Ports

```python
ports = client.symbol.read_ports("demoLib", "nand2")
```

The returned dictionary contains `terms`, `labels`, `selectionBoxes`,
`pinOrder`, `portOrder`, and `termOrder`. Each label record includes its text,
label type, layer, purpose, position, justification, orientation, font, height,
and bounding box. These fields matter because label type alone does not
identify a semantic symbol label: a normal label on `pin/label` is a pin name,
while a normal label on `annotate/drawing` is only drawing text.

`pinOrder` is the effective value from `schGetPinOrder()`, `portOrder` is the
native symbol property, and `termOrder` is retained separately for existing
callers and manually authored symbols.

## Edit Symbol

Use `client.symbol.edit()` as a context manager with the builders exported by
`virtuoso_bridge.virtuoso.symbol.ops`. Before saving on context exit, the
editor verifies that the open symbol can produce a pin list through Cadence's
symbol-specific `schSymbolToPinList()` API. Cadence rejects non-symbol
cellviews with `SCH-1004`, while unsuccessful pin-list generation also fails
the edit. This validation does not run schematic connectivity, SRC, or VIC;
`schCheck()` remains schematic-only.

### Drawing and semantic labels

The geometric builders create ordinary database shapes:

```python
from virtuoso_bridge.virtuoso.symbol import (
    symbol_create_ellipse,
    symbol_create_instance_label,
    symbol_create_label,
    symbol_create_line,
    symbol_create_logical_label,
    symbol_create_pin,
    symbol_create_pin_name,
    symbol_create_polygon,
    symbol_create_rect,
    symbol_create_selection_box,
    symbol_set_term_order,
)
```

`symbol_create_label()` is for non-semantic drawing text. Labels that
Virtuoso interprets on placed instances must use the dedicated builders. They
call `schCreateSymbolLabel`, which applies the current session's
`schSymbolLabelChoices` mapping:

| Meaning | Builder | Label choice | Default text | Type | Layer/purpose |
|---|---|---|---|---|---|
| Pin name | `symbol_create_pin_name` | `pin name` | pin name | `normalLabel` | `pin/label` |
| Instance name | `symbol_create_instance_label` | `instance label` | `[@instanceName]` | `NLPLabel` | `instance/label` |
| Logical/part name | `symbol_create_logical_label` | `logical label` | `[@partName]` | `NLPLabel` | `device/label` |

Do not create `[@instanceName]` or `[@partName]` as generic labels and then
set `labelType` manually. In particular, `ILLabel` is not the native type for
these two choices. It is normally used by analog annotation expressions such
as `cdsName()`, `cdsTerm()`, and `cdsParam()`.

`symbol_create_pin(..., label=True)` creates its visible name through the
native `pin name` choice. Use `label=False` only when placing the name
separately with `symbol_create_pin_name()`.

### Selection box

Every manually drawn symbol should contain one selection box so that placed
instances can be selected and preselected in a schematic:

```python
symbol.add(symbol_create_selection_box(-1.5, -1.0, 1.5, 1.0))
```

The builder creates a rectangle on `instance/drawing`, matching Cadence's
native symbol generator. Size it around the symbol pin origins and device
shapes; instance and logical labels do not need to enlarge it. The editor does
not infer this geometry because the caller controls the drawing.

### Complete manual symbol

```python
with client.symbol.edit("demoLib", "prettyBlock") as symbol:
    symbol.add(symbol_create_polygon(
        "device", "drawing",
        [(-1.0, -0.75), (-1.0, 0.75), (1.0, 0.0)],
    ))

    symbol.add(symbol_create_pin(
        "VIN", -1.5, 0.25,
        direction="input",
        label_x=-0.9,
        label_y=0.25,
    ))
    symbol.add(symbol_create_pin(
        "VOUT", 1.5, 0.0,
        direction="output",
        label_x=0.9,
        label_y=0.0,
        label_justification="centerRight",
    ))

    symbol.add(symbol_create_instance_label(0.0, 1.0))
    symbol.add(symbol_create_logical_label(0.0, -1.0))
    symbol.add(symbol_create_selection_box(-1.5, -0.75, 1.5, 0.75))
    symbol.add(symbol_set_term_order(["VIN", "VOUT"]))
```

After creation, inspect `client.symbol.read_ports()` rather than checking only
the visible strings. For a correct manually drawn symbol, verify that pin-name
labels are `pin/label + normalLabel`, the instance label is
`instance/label + NLPLabel`, the logical label is `device/label + NLPLabel`,
and exactly one `selectionBoxes` record is present.

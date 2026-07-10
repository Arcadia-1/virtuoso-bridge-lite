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
temporary view, validates its terminals, copies it to the requested symbol
view, and verifies the final view with `client.symbol.read_ports()`.

- `sort_pins` accepts `"alphanumeric"`, `"geometric"`, or `None`. A requested
  value temporarily overrides `ssgSortPins`; the previous value is restored.
- `overwrite` defaults to `False`. When `True`, the existing target is replaced
  only after temporary-view generation and terminal validation succeed.
- `SymbolGenerationResult.action` is `"created"` or `"replaced"`.
- `terminal_names` contains the generated terminal names.
- `term_order` uses explicit `portOrder`, then legacy `termOrder`, then the
  Cadence default order: output, inputOutput, input; names are sorted within
  each direction.

The overwrite path avoids GUI replacement dialogs, but it is not a rollback
transaction: a failure after the final database copy can leave the new symbol
in place.

## Read Ports

```python
ports = client.symbol.read_ports("demoLib", "nand2")
```

The returned dictionary contains `terms`, `labels`, `portOrder`, and
`termOrder`. `portOrder` is the native symbol property; `termOrder` is retained
separately for existing callers and manually authored symbols.

## Edit Symbol

Use `client.symbol.edit()` as a context manager with the builders exported by
`virtuoso_bridge.virtuoso.symbol.ops`. The editor runs `schCheck`, saves, and
closes the symbol on context exit.

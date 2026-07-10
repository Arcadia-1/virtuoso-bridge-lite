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

The returned dictionary contains `terms`, `labels`, `pinOrder`, `portOrder`,
and `termOrder`. `pinOrder` is the effective value from `schGetPinOrder()`,
`portOrder` is the native symbol property, and `termOrder` is retained
separately for existing callers and manually authored symbols.

## Edit Symbol

Use `client.symbol.edit()` as a context manager with the builders exported by
`virtuoso_bridge.virtuoso.symbol.ops`. The editor runs `schCheck`, saves, and
closes the symbol on context exit.

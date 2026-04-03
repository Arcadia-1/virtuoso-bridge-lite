# Layout Reference

## Edit Pattern

```python
with client.layout.edit(lib, cell, mode="a") as layout:
    layout.add_rect("M1", "drawing", (0, 0, 1, 0.5))
    layout.add_path("M2", "drawing", [(0, 0), (1, 0)], width=0.1)
    layout.add_label("M1", "pin", (0.5, 0.25), "VDD")
    layout.add_polygon("M3", "drawing", [(0, 0), (1, 0), (1, 1), (0.5, 1.5)])
    layout.add_instance("tsmcN28", "nch_ulvt_mac", (0, 0), "M0")
    layout.add_via("M1_M2", (0.5, 0.25))
    layout.add_mosaic("tsmcN28", "nch_ulvt_mac", (0, 0), rows=2, cols=4,
                      row_pitch=0.5, col_pitch=1.0)
```

- `mode="w"`: create new (overwrites)
- `mode="a"`: append to existing

## Read / Query

```python
client.layout.read_geometry(lib, cell)   # all shapes + instances as dicts
client.layout.list_shapes(lib, cell)     # shape summary
client.layout.read_summary(lib, cell)    # cell overview
```

## Control

```python
client.layout.fit_view()
client.layout.show_only_layers(["M1", "M2"])
client.layout.highlight_net("VDD")
client.layout.clear_current()            # delete all shapes (keep instances)
client.layout.clear_routing()            # delete routing metals only
client.layout.delete_shapes_on_layer(lib, cell, "M3", "drawing")
client.layout.delete_cell(lib, cell)
```

## Tips

- **Read before routing**: use `read_geometry()` to get real coordinates, don't guess from labels
- **Large edits**: split into chunks, first `mode="w"`, then `mode="a"` for subsequent batches
- **Via names**: query `techGetTechFile(cv)~>viaDefs` via `execute_skill()` if unsure
- **Mosaic pitch**: origin-to-origin spacing, not edge gap. Derive from measured bbox
- **Labels on metal**: anchor directly on the metal shape, not beside it
- **Screenshot after edits**: visually verify geometry, don't trust coordinates alone

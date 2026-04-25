# Layout Reference

## Edit Pattern

```python
from virtuoso_bridge.virtuoso.layout import (
    layout_create_rect as rect,
    layout_create_path as path,
    layout_create_label as label,
    layout_create_polygon as polygon,
    layout_create_param_inst as inst,
    layout_create_via_by_name as via,
    layout_create_simple_mosaic as mosaic,
)

with client.layout.edit(lib, cell, mode="a") as lay:
    lay.add(rect("M1", "drawing", 0, 0, 1, 0.5))
    lay.add(path("M2", "drawing", [(0, 0), (1, 0)], 0.1))
    lay.add(label("M1", "pin", 0.5, 0.25, "VDD", "centerCenter", "R0", "roman", 0.1))
    lay.add(polygon("M3", "drawing", [(0, 0), (1, 0), (1, 1), (0.5, 1.5)]))
    lay.add(inst("tsmcN28", "nch_ulvt_mac", "layout", "M0", 0, 0, "R0"))
    lay.add(via("M1_M2", 0.5, 0.25))
    lay.add(mosaic("tsmcN28", "nch_ulvt_mac", rows=2, cols=4,
                   row_pitch=0.5, col_pitch=1.0))
```

- `mode="w"`: create new (overwrites)
- `mode="a"`: append to existing

## Read / Query

```python
from virtuoso_bridge.virtuoso.layout import layout_read_geometry, layout_list_shapes, layout_read_summary

r = client.execute_skill(layout_read_geometry(lib, cell))
r = client.execute_skill(layout_list_shapes())
r = client.execute_skill(layout_read_summary(lib, cell))
```

## Control

```python
from virtuoso_bridge.virtuoso.layout import (
    layout_fit_view, layout_show_only_layers, layout_highlight_net,
    clear_current_layout, layout_clear_routing,
    layout_delete_shapes_on_layer, layout_delete_cell,
)

client.execute_skill(layout_fit_view())
client.execute_skill(layout_show_only_layers([("M1", "drawing"), ("M2", "drawing")]))
client.execute_skill(layout_highlight_net("VDD"))
client.execute_skill(clear_current_layout())
client.execute_skill(layout_delete_shapes_on_layer("M3", "drawing"))
client.execute_skill(layout_delete_cell(lib, cell))
```

## Tips

- **Read before routing**: use `layout_read_geometry()` to get real coordinates, don't guess from labels
- **Large edits**: split into chunks, first `mode="w"`, then `mode="a"` for subsequent batches
- **Via names**: query `techGetTechFile(cv)~>viaDefs` via `execute_skill()` if unsure
- **Mosaic pitch**: origin-to-origin spacing, not edge gap. Derive from measured bbox
- **Labels on metal**: anchor directly on the metal shape, not beside it
- **Screenshot after edits**: visually verify geometry, don't trust coordinates alone

## See also

- `references/layout-python-api.md` — Python API reference

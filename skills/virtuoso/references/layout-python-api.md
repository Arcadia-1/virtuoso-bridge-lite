# Layout Python API

Python wrapper for Cadence Virtuoso layout editing via SKILL.

**Package:** `virtuoso_bridge.virtuoso.layout`

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
# LayoutOps is accessed via client.layout
```

## LayoutEditor (context manager)

Collects SKILL commands, executes as a batch on `__exit__`, then saves automatically.

```python
from virtuoso_bridge.virtuoso.layout import (
    layout_create_rect as rect,
    layout_create_path as path,
    layout_create_param_inst as inst,
    layout_create_via_by_name as via,
)

with client.layout.edit(lib, cell) as lay:
    lay.add(rect("M1", "drawing", 0, 0, 1, 0.5))
    lay.add(path("M2", "drawing", [(0, 0), (1, 0)], 0.1))
    lay.add(inst("tsmcN28", "nch_ulvt_mac", "layout", "M0", 0, 0, "R0"))
    lay.add(via("M1_M2", 0.5, 0.25))
    # dbSave happens automatically on exit
```

### LayoutEditor methods

| Method | Description |
|--------|-------------|
| `add(skill_cmd)` | Queue a SKILL command (from ops functions) |
| `close()` | Append close-cellview command |

## SKILL builder functions (ops)

Use these with `lay.add(...)`:

**Create shapes:**

| Function | SKILL | Description |
|----------|-------|-------------|
| `layout_create_rect(layer, purpose, x1, y1, x2, y2)` | `dbCreateRect` | Rectangle |
| `layout_create_path(layer, purpose, points, width)` | `dbCreatePath` | Path with width |
| `layout_create_polygon(layer, purpose, points)` | `dbCreatePolygon` | Polygon |
| `layout_create_label(layer, purpose, x, y, text, just, rot, font, height)` | `dbCreateLabel` | Text label |

**Instances & vias:**

| Function | SKILL | Description |
|----------|-------|-------------|
| `layout_create_param_inst(lib, cell, view, name, x, y, orient)` | `dbCreateParamInst` | Place instance |
| `layout_create_simple_mosaic(lib, cell, *, origin, rows, cols, ...)` | `dbCreateSimpleMosaic` | Mosaic array |
| `layout_create_via(via_def_expr, x, y, orient, via_params)` | `dbCreateVia` | Via |
| `layout_create_via_by_name(via_name, x, y, ...)` | Via lookup + `dbCreateVia` | Via by name |

**Read:**

| Function | SKILL | Description |
|----------|-------|-------------|
| `layout_read_summary(lib, cell)` | Instance/shape count | Quick overview |
| `layout_read_geometry(lib, cell)` | Full geometry dump | Tab-separated output |
| `layout_list_shapes()` | Shape types and LPPs | From open window |

**Edit:**

| Function | SKILL | Description |
|----------|-------|-------------|
| `clear_current_layout()` | Delete visible shapes | Clear current |
| `layout_clear_routing()` | Delete all + save | Clear and save |
| `layout_select_box(bbox)` | `geSelectBox` | Select in box |
| `layout_delete_selected()` | `leDeleteAllSelect` | Delete selection |
| `layout_delete_shapes_on_layer(layer, purpose)` | Iterate + delete | Delete by layer |
| `layout_delete_cell(lib, cell)` | Close + `ddDeleteObj` | Delete cell |

**Layer visibility:**

| Function | SKILL | Description |
|----------|-------|-------------|
| `layout_set_active_lpp(layer, purpose)` | `leSetEntryLayer` | Set active layer |
| `layout_show_only_layers(layers)` | Hide all + show | Show specific LPPs |
| `layout_show_layers(layers)` | `leSetLayerVisible t` | Show LPPs |
| `layout_hide_layers(layers)` | `leSetLayerVisible nil` | Hide LPPs |
| `layout_highlight_net(net_name)` | `geSelectNet` | Highlight net |
| `layout_fit_view()` | `hiZoomAbsoluteScale` | Fit view |

## Utility

| Function | Description |
|----------|-------------|
| `parse_layout_geometry_output(raw)` | Parse `layout_read_geometry` output into `[{"kind": ..., "bbox": ..., ...}]` |
| `layout_find_via_def(via_name)` | Build SKILL to find via definition by name |
| `layout_via_def_expr_from_name(via_name)` | Build SKILL expr for via def lookup |

### Append mode

For large layouts, split into chunks:

```python
with client.layout.edit(lib, cell, mode="w") as lay:
    lay.add(rect("M1", "drawing", 0, 0, 10, 0.5))

with client.layout.edit(lib, cell, mode="a") as lay:
    lay.add(rect("M2", "drawing", 0, 1, 10, 1.5))
```

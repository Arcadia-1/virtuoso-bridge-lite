# Layout Python API

Python wrapper for Cadence Virtuoso layout editing via SKILL.

**Package:** `virtuoso_bridge.virtuoso.layout`

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
# LayoutOps is accessed via client.layout
```

## Export GDS with XStream Out

`client.layout.export_gds(...)` is the high-level XStream Out entry point. It
stages each run separately, waits for the current run to finish, publishes a
diagnostic log, and publishes the GDS only after the run is validated.

```python
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env()
result = client.layout.export_gds(
    "worklib",
    "top",
    Path("artifacts/top.gds"),
    stream_map=Path("pdk/stream.map"),
    view="layout",
    log_path=Path("artifacts/top.xstream.log"),
    timeout=300.0,
    poll_interval=0.5,
    skill_timeout=30.0,
    finalization_reserve=30.0,
    cleanup_policy="success",
)

if result.ok:
    print(f"GDS: {result.local_gds_path}")
else:
    print(f"{result.status.value}: {result.reason.value}")
    for error in result.errors:
        print(f"error: {error}")
```

The parameterized smoke example at
`examples/01_virtuoso/layout/15_export_gds.py` exposes the same controls.

### Paths, mode selection, and failures

- `output_path`, `log_path`, and `stream_map` are **caller-host paths**. The
  high-level API expands `~` and resolves them on the Python host. In remote
  mode it uploads the stream map and downloads the validated artifacts; do not
  pass compute-host-only paths for these arguments.
- Mode selection is strict: only `client.ssh_runner is None` selects the local
  path. Any non-`None` runner selects SSH mode. A missing or unreadable
  `ssh_runner` does not silently fall back to local operation; it produces a
  structured `TRANSPORT_ERROR` result.
- Input preflight happens before operational result construction. Empty design
  names, invalid timeout controls, aliased paths, an invalid cleanup policy, or
  `finalization_reserve >= timeout` raise `ValueError`; a stream map that is not
  an existing regular caller-host file raises `FileNotFoundError`.
- Once preflight succeeds, launch, staging, transport, XStream, timeout, and
  publication failures are returned as `GdsExportResult`. Inspect `result.ok`,
  `result.reason`, `result.errors`, and `result.warnings` instead of expecting
  those operational failures to raise.

`result.ok` is true only for `ExecutionStatus.SUCCESS`. Success means the
**current** XStream run has a parseable completion line with zero errors and a
fresh, non-empty GDS from the same staged run has been validated and published.
The parser ignores older concatenated runs by selecting the newest XStream
product anchor when present, or otherwise the newest start anchor. A
pre-existing destination GDS is never accepted as success evidence and is
preserved when the new GDS cannot be validated.

The log and GDS are each published through their own caller-host temporary file
and replacement. They are not a cross-directory transaction: a diagnostic log
may be available even when GDS publication fails, and the two destinations are
not promised to change atomically together.

### Result reason priority

Operational errors are selected before the observation classifier. Within the
classifier, the first matching row wins:

| Priority | Reason | Meaning |
|----------|--------|---------|
| Operational | `STAGING_ERROR` | Local staging creation or local staged-artifact observation failed. |
| Operational | `TRANSPORT_ERROR` | SSH command, upload/download, remote snapshot, digest, or remote integrity verification failed. |
| Operational | `PUBLICATION_ERROR` | Caller-host destination preparation, validation, or replacement failed. |
| 1 | `REQUEST_CLEANUP_ERROR` | The XStream request ran but restoring the prior XStream fields failed. |
| 2 | `XSTREAM_FAILURE` | The current log contains a terminal failure marker. |
| 3 | `MALFORMED_LOG` | A completion marker exists but its error/warning counts cannot be parsed. |
| 4 | `XSTREAM_ERRORS` | Completion counts are valid and report one or more errors. |
| 5 | `SKILL_ERROR` | The launch returned a definite, non-timeout SKILL/bridge error. |
| 6 | `MISSING_GDS` | A valid zero-error completion exists but no staged GDS exists. |
| 7 | `EMPTY_GDS` | A valid zero-error completion exists but the staged GDS is empty. |
| 8 | `INCOMPLETE_LOG` | No valid current completion exists, and either launch was determinate or current-run evidence appeared. |
| 9 | `LAUNCH_INDETERMINATE` | Launch timed out in an accepted indeterminate form and no run evidence appeared. |
| 10 | `COMPLETED` | Valid zero-error completion plus a validated, published current-run GDS. |

Explicit error lines remain available as diagnostics; completion counts and
terminal failure markers determine the XStream reason priority.

The three operational reasons identify the failed boundary: `STAGING_ERROR` is
caller-host staging, `TRANSPORT_ERROR` is the remote/transfer integrity path,
and `PUBLICATION_ERROR` is committing an artifact to its requested caller-host
destination.

### Cleanup and retention

| `cleanup_policy` | Successful result | Non-success result |
|------------------|-------------------|--------------------|
| `"success"` | Remove the run directory after publication. | Retain it for diagnosis. |
| `"always"` | Remove it when cleanup is safe. | Remove it only after a diagnostic log has been safely published locally (and, in SSH mode, downloaded from a stable remote snapshot). |
| `"never"` | Retain the run directory. | Retain the run directory. |

Cleanup is best effort and budgeted. A cleanup failure or exhausted cleanup
budget adds a warning without replacing the export status/reason. `"never"`
retains the run directory, but an unvalidated GDS inside it may still be removed
so it cannot be mistaken for a publishable result.

For SSH runs, `remote_files_retained` is deliberately three-state:

| Value | Meaning |
|-------|---------|
| `False` | Remote run-directory removal was confirmed. |
| `True` | The run directory is known to remain, including deliberate retention or a definitive cleanup failure. |
| `None` | Retention could not be determined, such as an indeterminate SSH/staging/cleanup outcome. It is also not applicable to a local-only run. |

An optional `recovery_hook` is considered only after an accepted indeterminate
launch timeout produces current-run progress. It runs at most once. Hook
exceptions become warnings and polling continues. The default is `None`, so the
flow is headless and never performs implicit X11/dialog recovery; normal
launches and timeouts with no artifact progress do not call the hook.

### Remote integrity and private staging

SSH mode creates and verifies an owner-only staging chain and run directory at
mode `0700`, rejecting symlinks or unexpected ownership. During finalization it
uses the first available remote SHA-256 command from `sha256sum`, `shasum`, or
`openssl` to compare stable remote snapshots with downloaded bytes. These tools
are runtime alternatives, not package installation dependencies. If none is
available, the export returns a structured `TRANSPORT_ERROR`; the missing tool
is not surfaced as a preflight exception.

## Pure XStream helpers

The request renderer and log parser are available without running a client:

```python
from virtuoso_bridge.virtuoso.layout import (
    XStreamExportRequest,
    parse_xstream_log,
    xstream_export_gds_skill,
)

request = XStreamExportRequest(
    library="worklib",
    top_cell="top",
    view="layout",
    stream_file="/scratch/run/output.gds",
    layer_map="/scratch/run/stream.map",
    log_file="/scratch/run/xstream.log",
    run_dir="/scratch/run",
)
skill_source = xstream_export_gds_skill(request)

log_result = parse_xstream_log(
    """Product : Virtuoso(R) XStream Out
INFO: Translating cellview worklib/top/layout as STRUCTURE top.
INFO (XSTRM-234): Translation completed. 0 error(s) and 0 warning(s) found.
"""
)
print(log_result.completed, log_result.error_count)
```

Both helpers are deterministic and pure: they perform no filesystem, client,
transport, clock, or sleep I/O. Request path strings are escaped into SKILL
exactly as supplied; they are not expanded, resolved, normalized, or rewritten.
`parse_xstream_log()` only inspects its text argument and scopes results to the
latest run anchor.

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

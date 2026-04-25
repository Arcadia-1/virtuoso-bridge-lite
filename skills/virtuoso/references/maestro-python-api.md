# Maestro Python API

Python wrapper for Cadence Maestro (ADE Assembler) SKILL functions.

**Package:** `virtuoso_bridge.virtuoso.maestro`

```python
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import (
    snapshot,                                     # one-shot setup snapshot
    filter_sdb_xml, filter_active_state_xml,      # XML filters (pure)
    read_results, export_waveform,                # post-sim consumption
    open_session, close_session, find_open_session,
    open_gui_session, close_gui_session, run_and_wait,
    save_setup, purge_maestro_cellviews,
    create_test, set_design, set_analysis,
    set_var, set_corner, set_env_option, set_sim_option,
    add_output, set_spec, run_simulation, ...    # writers — see "Write" below
)
```

## Two Session Modes

| | Background (`open_session`) | GUI (`open_gui_session`) |
|---|---|---|
| Lock file | Creates `.cdslck` | Creates `.cdslck` |
| Read config | Yes | Yes |
| Write config | Yes | Yes (needs `maeMakeEditable`) |
| Run simulation | Can start, but `close_session` cancels it | Yes |
| `run_and_wait` | Starts + callback never fires reliably | Starts + waits for completion |
| Close | `close_session` → lock removed | `close_gui_session` |

**Use background for read/write config. Use GUI for simulation.**

## Standard Simulation Flow

See **[simulation-flow.md](simulation-flow.md)** for the complete 8-step guide (clean sessions → open GUI → run → read results), common pitfalls, and optimization loop patterns.

## Session Management

`maestro/lifecycle.py`

| Python | SKILL | Description |
|--------|-------|-------------|
| `open_session(client, lib, cell) -> str` | `maeOpenSetup` | Background open, returns session string |
| `close_session(client, session)` | `maeCloseSession` | Background close |
| `find_open_session(client) -> str \| None` | `maeGetSessions` + `maeGetSetup` | Find first active session with valid test |
| `open_gui_session(client, lib, cell) -> str` | `deOpenCellView` + `maeMakeEditable` | GUI open (required for simulation) |
| `close_gui_session(client)` | `hiCloseWindow` | GUI close |
| `purge_maestro_cellviews(client)` | `dbPurgeCellView` | Clean stale internal locks before opening |

```python
session = open_session(client, "PLAYGROUND_AMP", "TB_AMP_5T_D2S_DC_AC")
# ... do work ...
close_session(client, session)
```

## Read — `snapshot()` (single entry)

`maestro/reader/snapshot.py`

The library's stance: **raw SKILL output is the canonical format** —
no Python-side alist→dict parsing.  ``snapshot()`` returns labeled
SKILL probe outputs verbatim; consumers (AI / scripts) read SKILL
alists directly, the same way they'd read XML or `.log` text.

```python
d = snapshot(client)
# d = {
#   "session": "fnxSessionN",       # davSession of focused window
#   "app": "assembler",
#   "lib": "...", "cell": "...", "view": "maestro",
#   "mode": "Editing", "unsaved": False,
#   "raw_sections": [
#     ('ddGetObj("LIB")~>readPath',                          '"/home/.../LIB"'),
#     ('maeGetSetup(?session "fnxSession18")',               '("TB_OTA")'),
#     ('maeGetEnabledAnalysis("TB_OTA" ?session ...)',       '("ac" "dc" "noise")'),
#     ('maeGetAnalysis("TB_OTA" "ac" ?session ...)',         '(("anaName" "ac") ...)'),
#     ...
#   ],
# }
```

Each ``raw_sections`` tuple is **(actual SKILL string we ran, raw
output)**.  The label IS the SKILL — no separate "function name" or
"description".

`snapshot()` always reads the **currently focused** maestro window
(``hiGetCurrentWindow()``).  Click the window first, or call
`open_session` / `open_gui_session` to bring one up.

### Disk dump: `snapshot(client, output_root="...")`

Adds the full disk dump on top of the same dict.  Layout:

```
{output_root}/{YYYYMMDD_HHMMSS}__{lib}__{cell}/
├── maestro.sdb                    raw Cadence sdb
├── state_from_sdb.xml             YAML-filtered subset
├── active.state                   raw per-test state
├── state_from_active_state.xml    YAML-filtered + stale-test "tombstone" removal
├── state_from_skill.txt           ~16 raw SKILL probe outputs in [label] value format
└── {history_name}/                newest run
    ├── {history_name}.log         OA library log
    └── {point_subdir}/.../netlist/input.scs + psf/spectre.out + psf/logFile
                                    per-point (all corners), packed via tar
```

The dict gains an ``output_dir`` field with the snapshot directory path.

Filtered XMLs use ``src/virtuoso_bridge/resources/snapshot_filter.yaml``
as the keep-list; edit that file to change which `<active>` children
or `<Test>` components are retained.

## Pure XML filters

`maestro/reader/_parse_sdb.py`

| Python | Input | Output |
|--------|-------|--------|
| `filter_sdb_xml(xml_text) → str` | raw `maestro.sdb` text | YAML-filtered XML (high-signal subset) |
| `filter_active_state_xml(xml_text, *, valid_test_names=None) → str` | raw `active.state` text | YAML-filtered XML; `valid_test_names` drops "tombstone" `<Test>` blocks for tests that no longer exist in sdb's `<active><tests>` |

Pure functions — no I/O, no client.  Useful when you've already
pulled the XML to disk by other means.

## Read — post-sim consumption

`maestro/reader/runs.py`

### read_results — per-point × per-output results

Internally calls `maeExportOutputView ?view "Detail"` to dump the
full Cadence result table to CSV, downloads it, parses into a
per-point structure.  This is the *all points × all outputs* view —
unlike `maeGetOutputValue` (only the currently-selected point) or
the `.log` summary (only the "best" point).

```python
results = read_results(client, session, lib="myLib", cell="myTB")
# {
#   "history": "Interactive.7",
#   "tests":   ["TB_OTA"],
#   "points":  [
#     {"point": 1,
#      "parameters": {"VDD": "0.9", "CONFIG/...": "calibre"},
#      "outputs":    {"Gain_dB": {"value": "21.63",
#                                  "spec": "", "weight": "",
#                                  "pass_fail": ""},
#                     ...}},
#     {"point": 2, ...},
#   ],
#   "outputs":       [...],   # back-compat flat list across points
#   "overall_spec":  "passed" | "failed" | None,
#   "overall_yield": "(nil Yield 100 PassedPoints 3 ...)" | None,
# }
```

GUI mode required (`maeOpenResults`).  Auto-detects the latest valid
history if `history=` not given.  Pass `include_raw=True` to attach
the raw exported CSV under `"raw_csv"`.

### export_waveform — OCEAN waveform export

```python
export_waveform(client, session,
    'dB20(mag(VF("/VOUT") / VF("/VSIN")))',
    "output/gain_db.txt", analysis="ac")

export_waveform(client, session,
    'getData("out" ?result "noise")',
    "output/noise.txt", analysis="noise")
```

Calls `maeOpenResults` → `selectResults` → `ocnPrint` → `maeCloseResults`,
then scp's the text file back.

## Write — Test

`maestro/writer.py`

| Python | SKILL | Description |
|--------|-------|-------------|
| `create_test(client, test, *, lib, cell, view="schematic", simulator="spectre", session="")` | `maeCreateTest` | Create a new test |
| `set_design(client, test, *, lib, cell, view="schematic", session="")` | `maeSetDesign` | Change DUT for existing test |

```python
create_test(client, "TRAN2", lib="myLib", cell="myCell")
set_design(client, "TRAN2", lib="myLib", cell="newCell")
```

## Write — Analysis

| Python | SKILL | Description |
|--------|-------|-------------|
| `set_analysis(client, test, analysis, *, enable=True, options="", session="")` | `maeSetAnalysis` | Enable/disable analysis, set options |

```python
# Enable transient with stop=60n
set_analysis(client, "TRAN2", "tran", options='(("stop" "60n") ("errpreset" "conservative"))')

# Enable AC
set_analysis(client, "TRAN2", "ac", options='(("start" "1") ("stop" "10G") ("dec" "20"))')

# Disable tran
set_analysis(client, "TRAN2", "tran", enable=False)
```

## Write — Outputs & Specs

| Python | SKILL | Description |
|--------|-------|-------------|
| `add_output(client, name, test, *, output_type="", signal_name="", expr="", session="")` | `maeAddOutput` | Add waveform or expression output |
| `set_spec(client, name, test, *, lt="", gt="", session="")` | `maeSetSpec` | Set pass/fail spec |

```python
# Waveform output
add_output(client, "OutPlot", "TRAN2", output_type="net", signal_name="/OUT")

# Expression output
add_output(client, "maxOut", "TRAN2", output_type="point", expr='ymax(VT(\\"/OUT\\"))')

# Spec: maxOut < 400mV
set_spec(client, "maxOut", "TRAN2", lt="400m")

# Spec: BW > 1GHz
set_spec(client, "BW", "AC", gt="1G")
```

## Write — Variables

| Python | SKILL | Description |
|--------|-------|-------------|
| `set_var(client, name, value, *, type_name="", type_value="", session="")` | `maeSetVar` | Set global variable or corner sweep |
| `get_var(client, name, *, session="")` | `maeGetVar` | Get variable value |

```python
set_var(client, "vdd", "1.35")
get_var(client, "vdd")  # => '"1.35"'

# Corner sweep
set_var(client, "vdd", "1.2 1.4", type_name="corner", type_value='("myCorner")')
```

## Write — Parameters (Parametric Sweep)

| Python | SKILL | Description |
|--------|-------|-------------|
| `get_parameter(client, name, *, type_name="", type_value="", session="")` | `maeGetParameter` | Read parameter value |
| `set_parameter(client, name, value, *, type_name="", type_value="", session="")` | `maeSetParameter` | Add/update parameter |

```python
set_parameter(client, "cload", "1p")
set_parameter(client, "cload", "1p 2p", type_name="corner", type_value='("myCorner")')
```

## Write — Environment & Simulator Options

| Python | SKILL | Description |
|--------|-------|-------------|
| `set_env_option(client, test, options, *, session="")` | `maeSetEnvOption` | Set model files, view lists, etc. |
| `set_sim_option(client, test, options, *, session="")` | `maeSetSimOption` | Set reltol, temp, gmin, etc. |

```python
# Change model file section
set_env_option(client, "TRAN2",
    '(("modelFiles" (("/path/model.scs" "ff"))))')

# Change temperature
set_sim_option(client, "TRAN2", '(("temp" "85"))')
```

## Write — Corners

| Python | SKILL | Description |
|--------|-------|-------------|
| `set_corner(client, name, *, disable_tests="", session="")` | `maeSetCorner` | Create/modify corner (empty) |
| `setup_corner(client, name, *, model_file="", model_section="", variables={}, session="")` | `maeSetCorner` + `maeSetVar` + `axl*` | **Recommended.** Create fully configured corner with model file, section, and variables — no XML editing |
| `load_corners(client, filepath, *, sections="corners", operation="overwrite")` | `maeLoadCorners` | Load corners from CSV |

```python
# Create a fully configured corner (recommended)
setup_corner(client, "tt_25",
             model_file="/path/to/mypdk.scs",
             model_section="tt",
             variables={"temperature": "25", "vdd": "1.2"},
             session=session)

# Create empty corner only
set_corner(client, "myCorner", disable_tests='("AC" "TRAN")')

# Load corners from CSV
load_corners(client, "my_corners.csv")
```

## Write — Run Mode & Job Control

| Python | SKILL | Description |
|--------|-------|-------------|
| `set_current_run_mode(client, run_mode, *, session="")` | `maeSetCurrentRunMode` | Switch run mode |
| `set_job_control_mode(client, mode, *, session="")` | `maeSetJobControlMode` | Set Local/LSF/etc. |
| `set_job_policy(client, policy, *, test_name="", job_type="", session="")` | `maeSetJobPolicy` | Set job policy |

```python
set_current_run_mode(client, "Single Run, Sweeps and Corners")
set_job_control_mode(client, "Local")
```

## Write — Simulation

| Python | SKILL | Description |
|--------|-------|-------------|
| `run_simulation(client, *, session="", callback="")` | `maeRunSimulation` | Run (async), returns history name |
| `run_and_wait(client, *, session="", timeout=600)` | `maeRunSimulation(?callback ...)` + SSH poll | **Recommended.** Run + wait without blocking SKILL channel |

```python
# Recommended: run_and_wait (no race condition, SKILL stays free)
history, status = run_and_wait(client, session=session, timeout=600)

# Or manual two-step (if you need custom callback):
# history = run_simulation(client, session=session)
# ... SKILL channel is free, do other work ...
```

## Write — Export

| Python | SKILL | Description |
|--------|-------|-------------|
| `create_netlist_for_corner(client, test, corner, output_dir)` | `maeCreateNetlistForCorner` | Export netlist for one corner |
| `export_output_view(client, filepath, *, view="Detail")` | `maeExportOutputView` | Export results to CSV |
| `write_script(client, filepath)` | `maeWriteScript` | Export setup as SKILL script |

```python
create_netlist_for_corner(client, "TRAN2", "myCorner_2", "./myNetlistDir")
export_output_view(client, "./results.csv")
write_script(client, "mySetupScript.il")
```

## Write — Migration

| Python | SKILL | Description |
|--------|-------|-------------|
| `migrate_adel_to_maestro(client, lib, cell, state)` | `maeMigrateADELStateToMaestro` | ADE L → Maestro |
| `migrate_adexl_to_maestro(client, lib, cell, view="adexl", *, maestro_view="maestro")` | `maeMigrateADEXLToMaestro` | ADE XL → Maestro |

```python
migrate_adel_to_maestro(client, "myLib", "myCell", "spectre_state1")
migrate_adexl_to_maestro(client, "myLib", "myCell")
```

## Write — Save

| Python | SKILL | Description |
|--------|-------|-------------|
| `save_setup(client, lib, cell, *, session="")` | `maeSaveSetup` | Save maestro to disk |

```python
save_setup(client, "myLib", "myCell", session=session)
```

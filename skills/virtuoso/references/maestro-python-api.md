# Maestro Python API

Python wrapper for Cadence Maestro (ADE Assembler) SKILL functions.

**Package:** `virtuoso_bridge.virtuoso.maestro`

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()
```

Use `client.maestro.*` for every Maestro operation that executes through
Virtuoso. Pure helpers such as `filter_sdb_xml`, `filter_active_state_xml`,
and `maestro_open_waveform_viewer_skill` remain standalone functions. The
historical `function(client, ...)` entry points are retained for compatibility.

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
| `client.maestro.open_session(lib, cell) -> str` | `maeOpenSetup` | Background open, returns session string |
| `client.maestro.close_session(session)` | `maeCloseSession` | Background close |
| `client.maestro.find_open_session() -> str \| None` | `maeGetSessions` + `maeGetSetup` | Find first active session with valid test |
| `client.maestro.open_gui_session(lib, cell, *, timeout=60) -> str` | `deOpenCellView` + `maeMakeEditable` | GUI open (required for simulation) |
| `client.maestro.close_gui_session(session, save=True, *, timeout=60)` | `hiCloseWindow` (+ `maeMakeEditable`/`dbPurge` as needed) | GUI close |
| `client.maestro.purge_maestro_cellviews(*, timeout=60)` | `dbPurgeCellView` | Clean stale internal locks before opening |

```python
session = client.maestro.open_session("PLAYGROUND_AMP", "TB_AMP_5T_D2S_DC_AC")
# ... do work ...
client.maestro.close_session(session)
```

**`timeout` kwarg** (`open_gui_session` / `close_gui_session` /
`purge_maestro_cellviews`): bounds each blocking SKILL call in the
helper. Default 60s — generous enough for cold maestro view opens
(P50 15-30s on busy servers) and the close-path's `dbPurge`. Pass a
larger value (e.g. `timeout=120`) on heavily loaded systems; pass a
smaller one only if you have your own retry/cancel logic.

## Read — `snapshot()` (single entry)

`maestro/reader/snapshot.py`

The library's stance: **raw SKILL output is the canonical format** —
no Python-side alist→dict parsing.  ``snapshot()`` returns labeled
SKILL probe outputs verbatim; consumers (AI / scripts) read SKILL
alists directly, the same way they'd read XML or `.log` text.

```python
d = client.maestro.snapshot()
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

`client.maestro.snapshot()` always reads the **currently focused** maestro window
(``hiGetCurrentWindow()``).  Click the window first, or call
`client.maestro.open_session` / `client.maestro.open_gui_session` to bring one up.

### Disk dump: `client.maestro.snapshot(output_root="...")`

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
    └── {point_subdir}/.../netlist/{input.scs,netlist,exprOutputs.json}
        + psf/spectre.out + psf/logFile
                                    per-point (all corners), packed via tar
```

The dict gains an ``output_dir`` field with the snapshot directory path.

Filtered XMLs use ``src/virtuoso_bridge/virtuoso/maestro/snapshot_filter.yaml``
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
results = client.maestro.read_results(session, lib="myLib", cell="myTB")
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
client.maestro.export_waveform(session,
    'dB20(mag(VF("/VOUT") / VF("/VSIN")))',
    "output/gain_db.txt", analysis="ac")

client.maestro.export_waveform(session,
    'getData("out" ?result "noise")',
    "output/noise.txt", analysis="noise")
```

Calls `maeOpenResults` → `selectResults` → `ocnPrint` → `maeCloseResults`,
then scp's the text file back.

### open_waveform_viewer — interactive ViVA/AWV plot

Open an interactive waveform window for explicit signals from a Maestro
history. The helper deliberately keeps its Maestro results session alive while
the plot is open; pass the returned window and session handles to
`client.maestro.close_waveform_viewer()` when finished.

```python
result = client.maestro.open_waveform_viewer(
    "myLib", "myTB", "Interactive.7", signals=["/OUT", "/IN"],
    result="tran",
)
# result.output encodes the retained Maestro session and waveform window.

client.maestro.close_waveform_viewer(window=12, session="fnxSession7")
```

Use `results_dir=` only when the raw PSF directory is known; in that mode a
failed `openResults()` is an error rather than a fallback to another active
result context.

## Write — Test

`maestro/writer.py`

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.create_test(test, *, lib, cell, view="schematic", simulator="spectre", session="")` | `maeCreateTest` | Create a new test |
| `client.maestro.set_design(test, *, lib, cell, view="schematic", session="")` | `maeSetDesign` | Change DUT for existing test |

```python
client.maestro.create_test("TRAN2", lib="myLib", cell="myCell")
client.maestro.set_design("TRAN2", lib="myLib", cell="newCell")
```

## Write — Analysis

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.set_analysis(test, analysis, *, enable=True, options="", session="")` | `maeSetAnalysis` | Enable/disable analysis, set options |

```python
# Enable transient with stop=60n
client.maestro.set_analysis("TRAN2", "tran", options='(("stop" "60n") ("errpreset" "conservative"))')

# Enable AC
client.maestro.set_analysis("TRAN2", "ac", options='(("start" "1") ("stop" "10G") ("dec" "20"))')

# Disable tran
client.maestro.set_analysis("TRAN2", "tran", enable=False)
```

## Write — Outputs & Specs

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.add_output(name, test, *, output_type="", signal_name="", expr="", session="")` | `maeAddOutput` | Add waveform or expression output |
| `client.maestro.set_spec(name, test, *, lt="", gt="", session="")` | `maeSetSpec` | Set pass/fail spec |

```python
# Waveform output
client.maestro.add_output("OutPlot", "TRAN2", output_type="net", signal_name="/OUT")

# Expression output
client.maestro.add_output("maxOut", "TRAN2", output_type="point", expr='ymax(VT(\\"/OUT\\"))')

# Spec: maxOut < 400mV
client.maestro.set_spec("maxOut", "TRAN2", lt="400m")

# Spec: BW > 1GHz
client.maestro.set_spec("BW", "AC", gt="1G")
```

## Write — Variables

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.set_var(name, value, *, type_name="", type_value="", session="")` | `maeSetVar` | Set global variable or corner sweep |
| `client.maestro.get_var(name, *, session="")` | `maeGetVar` | Get variable value |

```python
client.maestro.set_var("vdd", "1.35")
client.maestro.get_var("vdd")  # => '"1.35"'

# Corner sweep
client.maestro.set_var("vdd", "1.2 1.4", type_name="corner", type_value='("myCorner")')
```

## Write — Parameters (Parametric Sweep)

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.get_parameter(name, *, type_name="", type_value="", session="")` | `maeGetParameter` | Read parameter value |
| `client.maestro.set_parameter(name, value, *, type_name="", type_value="", session="")` | `maeSetParameter` | Add/update parameter |

```python
client.maestro.set_parameter("cload", "1p")
client.maestro.set_parameter("cload", "1p 2p", type_name="corner", type_value='("myCorner")')
```

## Write — Environment & Simulator Options

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.set_env_option(test, options, *, session="")` | `maeSetEnvOption` | Set model files, view lists, etc. |
| `client.maestro.set_sim_option(test, options, *, session="")` | `maeSetSimOption` | Set reltol, temp, gmin, etc. |

```python
# Change model file section
client.maestro.set_env_option("TRAN2",
    '(("modelFiles" (("/path/model.scs" "ff"))))')

# Change temperature
client.maestro.set_sim_option("TRAN2", '(("temp" "85"))')
```

## Write — Corners

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.set_corner(name, *, disable_tests="", session="")` | `maeSetCorner` | Create/modify corner (empty) |
| `client.maestro.setup_corner(name, *, model_file="", model_section="", variables={}, session="")` | `maeSetCorner` + `maeSetVar` + `axl*` | **Recommended.** Create fully configured corner with model file, section, and variables — no XML editing |
| `client.maestro.load_corners(filepath, *, sections="corners", operation="overwrite")` | `maeLoadCorners` | Load corners from CSV |

```python
# Create a fully configured corner (recommended)
client.maestro.setup_corner("tt_25",
             model_file="/path/to/mypdk.scs",
             model_section="tt",
             variables={"temperature": "25", "vdd": "1.2"},
             session=session)

# Create empty corner only
client.maestro.set_corner("myCorner", disable_tests='("AC" "TRAN")')

# Load corners from CSV
client.maestro.load_corners("my_corners.csv")
```

## Write — Run Mode & Job Control

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.set_current_run_mode(run_mode, *, session="")` | `maeSetCurrentRunMode` | Switch run mode |
| `client.maestro.set_job_control_mode(mode, *, session="")` | `maeSetJobControlMode` | Set Local/LSF/etc. |
| `client.maestro.set_job_policy(policy, *, test_name="", job_type="", session="")` | `maeSetJobPolicy` | Set job policy |

```python
client.maestro.set_current_run_mode("Single Run, Sweeps and Corners")
client.maestro.set_job_control_mode("Local")
```

## Write — Simulation

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.run_simulation(*, session="", callback="")` | `maeRunSimulation` | Run (async), returns history name |
| `client.maestro.run_and_wait(*, session="", timeout=600)` | `maeRunSimulation(?callback ...)` + SSH poll | **Recommended.** Run + wait without blocking SKILL channel |

```python
# Recommended: run_and_wait (no race condition, SKILL stays free)
history, status = client.maestro.run_and_wait(session=session, timeout=600)

# Or manual two-step (if you need custom callback):
# history = client.maestro.run_simulation(session=session)
# ... SKILL channel is free, do other work ...
```

## Write — Export

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.create_netlist_for_corner(test, corner, output_dir, *, session="")` | `maeCreateNetlistForCorner` | Export netlist for one corner, optionally from an explicit session |
| `client.maestro.export_output_view(filepath, *, view="Detail")` | `maeExportOutputView` | Export results to CSV |
| `client.maestro.write_script(filepath)` | `maeWriteScript` | Export setup as SKILL script |

```python
client.maestro.create_netlist_for_corner(
    "TRAN2",
    "myCorner_2",
    "./myNetlistDir",
    session=session,
)
client.maestro.export_output_view("./results.csv")
client.maestro.write_script("mySetupScript.il")
```

## Write — Migration

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.migrate_adel_to_maestro(lib, cell, state)` | `maeMigrateADELStateToMaestro` | ADE L → Maestro |
| `client.maestro.migrate_adexl_to_maestro(lib, cell, view="adexl", *, maestro_view="maestro")` | `maeMigrateADEXLToMaestro` | ADE XL → Maestro |

```python
client.maestro.migrate_adel_to_maestro("myLib", "myCell", "spectre_state1")
client.maestro.migrate_adexl_to_maestro("myLib", "myCell")
```

## Write — Save

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.save_setup(lib, cell, *, session="")` | `maeSaveSetup` | Save maestro to disk |

```python
client.maestro.save_setup("myLib", "myCell", session=session)
```

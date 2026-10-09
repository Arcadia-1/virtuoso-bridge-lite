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

See **[simulation-flow.md](simulation-flow.md)** for the standard sequence
(clean sessions → open GUI → run → read results), common pitfalls, and
optimization loop patterns.

## Session Management

`maestro/lifecycle.py`

| Python | SKILL | Description |
|--------|-------|-------------|
| `client.maestro.open_session(lib, cell) -> str` | `maeOpenSetup` | Background open, returns session string |
| `client.maestro.close_session(session)` | `maeCloseSession` | Background close |
| `client.maestro.find_open_session() -> str \| None` | `maeGetSessions` + `maeGetSetup` | Find first active session with valid test |
| `client.maestro.open_gui_session(lib, cell, *, timeout=60) -> str` | `deOpenCellView(..., "a")` | GUI open directly in editable mode (required for simulation) |
| `client.maestro.close_gui_session(session, save=True, *, timeout=60)` | `maeSaveSetup` + `hiCloseWindow` + `dbPurge` as needed | Fail-closed GUI close |
| `client.maestro.purge_maestro_cellviews(*, timeout=60)` | `dbPurgeCellView` | Clean stale internal locks before opening |
| `client.maestro.get_session_state(session=None, *, timeout=30)` | Atomic window/session inventory | Exact session state, or current-window context |
| `client.maestro.list_session_states(*, timeout=30)` | Atomic window/session inventory | All observed GUI and headless sessions |
| `client.maestro.list_histories(session)` | `axlGetHistory` | List exact names and lock/current state |
| `client.maestro.get_history(history, session=session)` | `axlGetHistoryEntry` | Read one exact history |
| `client.maestro.set_history_lock(history, locked, session=session)` | `maeSetHistoryLock` | Set and verify an explicit lock state |
| `client.maestro.lock_history(history, session=session)` | `maeSetHistoryLock` | Idempotently lock one history |
| `client.maestro.unlock_history(history, session=session)` | `maeSetHistoryLock` | Idempotently unlock one history |

```python
session = client.maestro.open_session("PLAYGROUND_AMP", "TB_AMP_5T_D2S_DC_AC")
# ... do work ...
client.maestro.close_session(session)
```

### Structured session state

Use the state API before any workflow that may save, discard, close, or make a
Maestro window editable:

```python
state = client.maestro.get_session_state(session)
print(state.context, state.access, state.unsaved)

for state in client.maestro.list_session_states():
    print(state.session, state.lib, state.cell, state.access)
```

`context` is one of `gui`, `headless`, `no_window`,
`non_maestro_window`, `not_found`, or `unknown`. `access` is `editing`,
`reading`, or `unknown`. An `unsaved` value of `None` means the condition is
not observable; it must not be treated as clean. The returned object also
contains the exact window/session identity, parsed library/cell/view, raw
title, evidence source, and diagnostics.

The inventory binds windows through `axlGetWindowSession` and `davSession`,
then parses known ADE title shapes. A session without an observed window is
reported as `headless`; the probe does not claim whether it is a deliberate
background session or a stale GUI session. Malformed/failed probes raise
`MaestroStateProbeError` instead of being reported as an empty inventory.

### Preserve simulation histories

Maestro normally retains only a bounded number of recent histories. Lock an
important history to prevent its setup details and simulation results from
being automatically deleted:

```python
histories = client.maestro.list_histories(session)
for history in histories:
    print(history.name, history.locked, history.current)

result = client.maestro.lock_history("Interactive.7", session=session)
# Later, when retention is no longer required:
client.maestro.unlock_history("Interactive.7", session=session)
```

All operations require an explicit session and exact history name. Lock and
unlock are idempotent, use `maeSetHistoryLock`, and verify the database state
after the mutation. A timeout or connection interruption is never retried; if
one read-back cannot confirm the requested state,
`MaestroHistoryOutcomeUnknown` is raised.

### Monte Carlo configuration and mismatch isolation

The structured Monte Carlo API uses an explicit Maestro session and supports
global (process), mismatch, or combined variation, deterministic seeds, sampling, result
retention, and hierarchical DUT filters:

```python
from virtuoso_bridge.virtuoso.maestro import (
    MonteCarloConfig,
    MonteCarloModule,
    MonteCarloModuleFilter,
)

config = MonteCarloConfig(
    variation="mismatch",
    points=200,
    seed=12345,
    save_mismatch=True,
    module_filter=MonteCarloModuleFilter(
        mode="include",
        modules=(
            MonteCarloModule(
                test="TRAN",
                instance="/I_CLK",
                master="myLib/clk_gen/schematic",
                kind="Master",
            ),
        ),
    ),
)

# Validate the exact target and show the planned SKILL without changing it.
plan = client.maestro.configure_monte_carlo(
    config, session=session, dry_run=True,
)

# Apply once, save, then read back both options and run mode.
applied = client.maestro.configure_monte_carlo(config, session=session)

# Run and protect the resulting history from Maestro retention cleanup.
run = client.maestro.run_monte_carlo_and_wait(
    session=session, timeout=1800, lock_result=True,
)

# Export scalar results on the Virtuoso host, one CSV per corner.
client.maestro.export_monte_carlo_results(
    run.history, "/tmp/mc-results/", session=session,
)
```

The exporter passes `?outputPath` to `axlWriteMonteCarloResultsCSV`.  IC6.1.8's
installed reference table incorrectly labels this keyword as `?outputName`,
while both the installed example and the executable function require
`?outputPath`.

`module_filter.mode="include"` applies mismatch only to the listed
`dutSummary` entries; `"exclude"` applies mismatch everywhere except those
entries. Process variation remains global. Use the same seed when comparing
include/exclude configurations so differences are attributable to the module
selection. Group or binary partitioning is usually faster than testing every
module individually.

Configuration reads and writes use the setup database for the explicit
`session`: `axlGetRunOptions`/`axlGetRunOptionValue` for reads and
`axlPutRunOption`/`axlSetRunOptionValue` for writes. This also handles optional
run options such as `donominal`, `dutsummary`, and `ignoreflag` when they have
not yet been materialized in a setup. It does not depend on the focused Maestro
window or the session-global `ocnxlMonteCarloOptions` command.

The target must be one editable Maestro GUI session with no pre-existing
unsaved setup changes. Set `save=False` for temporary experiments; closing the
session without saving discards those option changes. A timeout or socket loss
is never retried. Configuration persistence that cannot be confirmed, or a run
whose start/completion acknowledgement is lost, raises
`MaestroMonteCarloOutcomeUnknown`; inspect the live setup or history list before
deciding whether to issue another mutation. `get_monte_carlo_config()` returns
`None` when no Monte Carlo run options are configured, rather than confusing
that state with default values.

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
    └── {point_subdir}/.../netlist/
        {netlist,input.scs,qpInformation.ils,exprOutputs.json,paramInfo.ils}
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
    "output/gain_db.txt",
    analysis="ac",
    history="Interactive.7",
    precision=12,
    width=20,
    number_notation="none",
)

client.maestro.export_waveform(session,
    'getData("out" ?result "noise")',
    "output/noise.txt", analysis="noise")
```

Calls `maeOpenResults` → `selectResults` → `ocnPrint`, then downloads the
text file through the configured transport and removes the collision-safe remote
temporary file. Pass `history=` when you already have the value returned by
`run_and_wait()` so the export cannot drift to a newer run.

Formatting controls are validated before any SKILL is sent:

- `precision` must be 1–16 significant digits.
- `width` must be at least 4 characters; use a value at least as large as
  `precision` when all requested digits must be visible.
- `number_notation` accepts `"suffix"`, `"engineering"`, `"scientific"`, or
  `"none"`. The default is `"scientific"`; `"none"` skips per-value formatting
  and is substantially faster for large exports.

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
client.maestro.add_output("maxOut", "TRAN2", output_type="point", expr='ymax(VT("/OUT"))')

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
| `client.maestro.run_simulation(*, session="", callback="", timeout=None)` | `maeRunSimulation` | Run (async), returns history name; `timeout` bounds acceptance of the run request |
| `client.maestro.run_and_wait(*, session="", timeout=600)` | `maeRunSimulation(?callback ...)` + SSH poll | **Recommended.** Run + wait without blocking SKILL channel; `timeout` is one end-to-end budget for request acceptance and completion polling |

```python
# Recommended: run_and_wait (no race condition, SKILL stays free)
history, status = client.maestro.run_and_wait(session=session, timeout=600)

# Or manual two-step (if you need custom callback):
# history = client.maestro.run_simulation(session=session)
# ... SKILL channel is free, do other work ...
```

### Durable asynchronous jobs

Use `MaestroJobManager` when a run must outlive the submitting Python process.
It registers a Cadence completion callback, sends `maeRunSimulation` exactly
once, and stores a local manifest keyed by `run_id`. Later `status()` and
`log()` calls read small files from the Virtuoso GUI host; they do not send
SKILL or require the original `VirtuosoClient` object.

```python
from pathlib import Path
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import MaestroJobManager

root = Path("results/maestro-jobs")
client = VirtuosoClient.from_env(profile="lab")
jobs = MaestroJobManager.from_client(client, local_root=root, profile="lab")
job = jobs.submit(session=session, run_id="long-tran-001")
client.close()

# A later Python process can reattach without contacting the CIW.
with MaestroJobManager.from_env(local_root=root, profile="lab") as jobs:
    job = jobs.load("long-tran-001")
    status = jobs.status(job)
    print(status.state, status.history)
```

For a genuinely local Virtuoso GUI, explicitly authorize the local filesystem
transport. A loopback TCP endpoint alone is not proof that the GUI is local:

```python
jobs = MaestroJobManager.from_client(
    client,
    local_gui=True,
    local_root=root,
)
```

Later use `MaestroJobManager.local()` to observe that saved handle. Do not pass
`local_gui=True` for an SSH-forwarded or otherwise remote TCP endpoint.

Submission requires an exact Maestro GUI session in Editing mode and enables
the shared-CIW dialog guard with recoverable in-flight requests. If start
acknowledgement is lost, `MaestroJobSubmissionError.state` is `unknown` and the
exception retains the generated `run_id`; the start request is never repeated.
The manifest also retains the authenticated request handle when one was issued.
Reuse the existing authenticated client for receipt recovery. In another process,
reconstruct a receipt-only client from the original reachable endpoint and secured
credential, as described in [shared-CIW recovery](shared-ciw-dialogs.md).
Ordinary `from_env()`/`from_tunnel()` factories may issue SKILL identity probes;
they are not a blocked-CIW recovery path. With the receipt-only `client` already
available, reconcile without submitting another run:

```python
with MaestroJobManager.from_env(local_root=root, profile="lab") as jobs:
    job = jobs.load("long-tran-001")
    if job.request_handle is not None:
        job = jobs.reconcile(job, client=client)
```

Receipt recovery is bounded by the daemon's retention and identity. An expired
receipt, daemon restart, changed endpoint, or unverifiable result remains
`unknown`; it is never treated as permission to repeat `maeRunSimulation`.
Preflight session-state/callback-setup receipts are retained too. Reconciliation
records their original result without resuming submission or interpreting `t`
as a simulation history. Once that preflight request is settled, the job is
`failed` because no simulation was started; starting a new job is a separate action.

Durable handles bind the configured and observed GUI host, remote account, job
namespace, Virtuoso PID, and process start identity. `status()`, `log()`, and
`reconcile()` reject host, account, or namespace drift before reading remote
files. Profile names are labels, not transport identity. Schema-1 development
manifests are rejected by default; `load(run_id, migrate_legacy=True)` is the
explicit opt-in for binding one to the currently selected endpoint. Migrated
handles do not use PID liveness without original process-start evidence.

`status().state` is one of `running`, `completed`, `failed`, `missing`, or
`unknown`. These are observations rather than a monotonic state machine:
`unknown` or `missing` can later become `completed` when the callback marker is
visible. A `completed` status only means the Cadence callback ran; it does not
prove simulator success, output specs, or design acceptance.

The lifecycle log contains only submitted/completed events. Read Maestro
history and simulator output separately for result validation.

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

---
name: virtuoso
description: "Bridge to remote Cadence Virtuoso via Python API. TRIGGER when user mentions: Virtuoso, Maestro, ADE, CIW, SKILL, layout, schematic, cellview, OCEAN, or any Cadence EDA operation."
---

# Virtuoso Skill

> **CRITICAL: Do NOT invent SKILL code or API calls from memory.**
> Before writing any SKILL expression or calling any Python API function:
> 1. **Search `references/`** for the function name or keyword
> 2. **Check `examples/`** for a working example of the same operation
> 3. **Read the actual function signature** (`help()` for Python, `references/*.md` for SKILL)
>
> If the function is not documented in references or examples, it probably does not exist
> or has a different name. Never guess parameter names -- verify first.

## Mental Model

You control a remote Cadence Virtuoso through `virtuoso-bridge`. Python runs locally; SKILL executes remotely in the Virtuoso CIW. SSH tunneling is automatic.

```
 Local (Python)                    Remote (Virtuoso)
┌──────────────────┐   SSH tunnel  ┌──────────────────┐
│ VirtuosoClient   │ ────────────► │ CIW (SKILL)      │
│                  │               │                  │
│ • schematic.*    │               │ • dbCreateInst   │
│ • layout.*       │               │ • schCreateWire  │
│ • execute_skill  │               │ • mae*           │
│ • load_il        │               │ • dbOpenCellView │
└──────────────────┘               └──────────────────┘
```

### Three abstraction levels

| Level | When to use | Example |
|-------|-------------|---------|
| **Python API** | Schematic/layout editing — structured, safe | `client.schematic.edit(lib, cell)` |
| **Inline SKILL** | Maestro, CDF params, anything the API doesn't cover | `client.execute_skill('maeRunSimulation()')` |
| **SKILL file** | Bulk operations, complex loops | `client.load_il("my_script.il")` |

Always use the highest level that works. Drop to a lower level only when needed.

**Never guess function names.** If the function isn't in the examples below, read the relevant `references/` file before writing the call. Fabricating a wrong name wastes time debugging in CIW.

### Four domains

| Domain | What it does | Python package | API docs |
|--------|-------------|----------------|----------|
| **Schematic** | Create/edit schematics, wire instances, add pins | `client.schematic.*` | `references/schematic-python-api.md`, `references/schematic-skill-api.md` |
| **Layout** | Create/edit layout, add shapes/vias/instances | `client.layout.*` | `references/layout-python-api.md`, `references/layout-skill-api.md` |
| **Maestro** | Read/write ADE Assembler config, run simulations | `virtuoso_bridge.virtuoso.maestro` | `references/maestro-python-api.md`, `references/maestro-skill-api.md` |
| **Netlist (si)** | Batch netlist generation without Maestro | `simInitEnvWithArgs` + `si` CLI | See "Batch Netlist (si)" section below |
| **General** | File transfer, screenshots, raw SKILL, .il loading | `client.*` | See below |

## Before you start

### Environment setup

> **`virtuoso-bridge` is a Python CLI.** Use `uv` + virtual environment — never install into the global Python.

```bash
uv venv .venv && source .venv/bin/activate   # Windows: source .venv/Scripts/activate
uv pip install -e virtuoso-bridge-lite
```

All `virtuoso-bridge` CLI commands and Python scripts must run inside the activated venv.

### Connection sequence (follow in order)

1. **Check `.env`** — the bridge looks up `.env` in this order: `--env FILE` (CLI flag) → `./.env` (project-local) → `~/.virtuoso-bridge/.env` (user-level). If **any** of these exists, skip `init`. Only run **`virtuoso-bridge init`** when none exist — it creates `~/.virtuoso-bridge/.env` (user-level, shared across projects). If the user already told you their SSH target, prefer `virtuoso-bridge init user@host [-J user@jump]` to fill host/user/jump + port in one step; otherwise plain `virtuoso-bridge init` writes an empty template for them to edit.
2. **`virtuoso-bridge start`** — starts the local bridge service and SSH tunnel.
3. **If status is `degraded`** — the user must load the setup script in Virtuoso CIW (the `start` output tells them exactly what to run).
4. **`virtuoso-bridge status`** — verify everything is `healthy` before proceeding.
5. **`virtuoso-bridge windows`** — list all open Virtuoso windows (num + name).
6. **`virtuoso-bridge screenshot [ciw|current|N]`** — screenshot a window to `output/`. Default: CIW.
7. **`virtuoso-bridge snapshot -o <dir>`** — dump the currently-focused maestro window to `<dir>/<YYYYMMDD_HHMMSS>__<lib>__<cell>/` (state XMLs, SKILL probe output, per-point netlist + PSF results, `.rdb`). This is the default way to capture Maestro state — no Python required. Use the Python API (below) only inside a multi-step pipeline.

### Then

- **Check examples first**: `examples/01_virtuoso/` — don't reinvent from scratch.
- **Open the window**: `client.open_window(lib, cell, view="layout")` so the user sees what you're doing.

## Client basics

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()

client.execute_skill('...')                     # run SKILL expression
client.fetch(expr, fields)                       # batch ~>slot extract (see below)
client.fetch_one(expr, fields)                   # single-object ~>slot extract
client.load_il("my_script.il")                  # upload + load .il file
client.upload_file(local_path, remote_path)      # local → remote
client.download_file(remote_path, local_path)    # remote → local
client.open_window(lib, cell, view="layout")     # open GUI window
client.run_shell_command("ls /tmp/")             # run shell on remote
client.list_windows()                            # list all open windows
client.screenshot(output="output", target="ciw") # screenshot a window
```

### Batch attribute fetch: `fetch()` / `fetch_one()`

`execute_skill()` is a raw-string in, raw-string out channel. For DFII
objects it returns an opaque handle (`"db:0x2800ccbe"`) that's useless
by itself — to get attributes you'd have to send another SKILL call
per attribute, which is both verbose and slow (~100 ms per
round-trip).

**`fetch(expr, fields)`** does the right thing in one round-trip:
sends `mapcar(lambda((o) list(o~>f1 o~>f2 ...)) <expr>)`, parses the
SKILL s-expression response, and returns a list of Python dicts.

```python
# List of selected schematic objects in one call
objs = client.fetch("geGetSelSet()", ["objType", "cellName", "name"])
# [{"objType": "inst", "cellName": "nch_mac", "name": "M1"},
#  {"objType": "inst", "cellName": "pch_mac", "name": "M2"}, ...]
print(objs[0]["name"])     # → 'M1'

# All instances in the current schematic — 1 call, not N×fields
insts = client.fetch(
    "geGetEditCellView()~>instances",
    ["name", "cellName", "libName", "viewName"],
)
```

`fetch_one(expr, fields)` is the single-object variant — wraps in
`list(...)` and returns one dict:

```python
cv = client.fetch_one("geGetEditCellView()",
                      ["libName", "cellName", "viewName"])
# {"libName": "PLAYGROUND", "cellName": "AMP", "viewName": "schematic"}
```

**Value decoding** (both methods): strings unquoted, ``nil`` →
``None``, ``t`` → ``True``, nested SKILL lists → nested Python lists,
bare atoms (numbers / symbols) returned as strings so the caller can
coerce (`int(d["fingers"])`).

**Why not a `client["fn"]()` lazy-proxy style (à la `skillbridge`)?**
Lazy proxies look nicer syntactically but trigger one round-trip per
attribute access — 100 selected objects × 3 fields = 300 ssh hops
(~30 s). `fetch` does it all in one hop (~200 ms). If you need the
REPL-style ergonomics, use `skillbridge` alongside this bridge —
they coexist fine on the same Virtuoso session.

### CIW output vs return value

`execute_skill()` returns the result to Python but does **not** print anything in the CIW window. This is by design — the bridge is a programmatic API, not an interactive REPL.

```python
# Return value only — CIW stays silent
r = client.execute_skill("1+2")        # Python gets 3, CIW shows nothing

# To also display in CIW, use printf explicitly
r = client.execute_skill(r'let((v) v=1+2 printf("1+2 = %d\n" v) v)')
#   Python gets 3, CIW shows "1+2 = 3"
```

Full example: `examples/01_virtuoso/basic/00_ciw_output_vs_return.py`

## Printing multi-line text to CIW

Sending multiple `printf` in a single `execute_skill()` loses newlines — the CIW concatenates everything on one line. To print multi-line text, write it as a Python multiline string and send one `execute_skill()` per line:

```python
text = """\
========================================
  Title goes here
========================================
  First paragraph line one.
  First paragraph line two.

  Second paragraph.
========================================"""

for line in text.splitlines():
    client.execute_skill('printf("' + line + '\\n")')
```

Constraints:
- **ASCII only** — emojis and CJK characters cause a JSON encoding error on the remote SKILL interpreter
- **No unescaped SKILL special chars** in the text — if the line may contain `"` or `%`, escape them (`\\"`, `%%`) or use `load_il()` instead (see `03_load_il.py`)

> **IMPORTANT: Always write `.py` files, never use `python -c`.**
> `python -c "..."` has three layers of quoting (shell + Python + SKILL). `\\n` easily becomes `\\\\n`, causing `printf` to silently produce no output.
> Always write code to a `.py` file and run `python script.py` -- only two quoting layers (Python + SKILL), matching the examples.

Full example: `examples/01_virtuoso/basic/02_ciw_print.py`

## References

Load on demand — each contains detailed API docs and edge-case guidance:

| File | Contents |
|------|----------|
| `references/schematic-skill-api.md` | Schematic SKILL API, terminal-aware helpers, CDF params |
| `references/schematic-python-api.md` | SchematicEditor, SchematicOps, low-level builders |
| `references/layout-skill-api.md` | Layout SKILL API, read/query, mosaic, layer control |
| `references/layout-python-api.md` | LayoutEditor, LayoutOps, shape/via/instance creation |
| `references/maestro-skill-api.md` | mae* SKILL functions, OCEAN, corners, known blockers |
| `references/maestro-python-api.md` | snapshot() (raw SKILL sections) + filter_*_xml + writer functions; read_results (per-point × per-output CSV) + export_waveform (OCEAN) |
| `references/simulation-flow.md` | **Standard simulation flow** — 8-step guide, pitfalls, optimization loops |
| `references/netlist.md` | CDL/Spectre netlist formats, spiceIn import |
| `references/troubleshooting.md` | Known gotchas, GUI blocking, CDF quirks, connection issues |
| `references/cellview-on-disk-layout.md` | What's inside each view on disk (`sch.oa`, `data.dm` binary format, `maestro.sdb`/`active.state` XML skeleton, lock files, SOS markers); which files are text-editable vs must go through DFII API |
| `references/schematic-recreation.md` | Recreate schematic from existing design (grid layout, diff pair conventions) |
| `references/batch-netlist-si.md` | Generate netlists without Maestro using si batch translator |

## Examples

**Always check these before writing new code.**

### `examples/01_virtuoso/basic/`
- `00_ciw_output_vs_return.py` — CIW output vs Python return value (when CIW prints, when it doesn't)
- `01_execute_skill.py` — run arbitrary SKILL expressions
- `02_ciw_print.py` — print messages to CIW (one `execute_skill` per line)
- `03_load_il.py` — upload and load .il files
- `04_list_library_cells.py` — list libraries and cells
- `05_multiline_skill.py` — multi-line SKILL with comments, loops, procedures
- `06_screenshot.py` — capture layout/schematic screenshots

### `examples/01_virtuoso/schematic/`
- `01a_create_rc_stepwise.py` — create RC schematic via operations
- `01b_create_rc_load_skill.py` — create RC schematic via .il script
- `02_read_connectivity.py` — read instance connections and nets
- `03_read_instance_params.py` — read CDF instance parameters
- `05_rename_instance.py` — rename schematic instances
- `06_delete_instance.py` — delete instances
- `07_delete_cell.py` — delete cells from library
- `08_import_cdl_cap_array.py` — import CDL netlist via spiceIn (SSH)

### `examples/01_virtuoso/layout/`
- `01_create_layout.py` — create layout with rects, paths, instances
- `02_add_polygon.py` — add polygons
- `03_add_via.py` — add vias
- `04_multilayer_routing.py` — multi-layer routing
- `05_bus_routing.py` — bus routing
- `06_read_layout.py` — read layout shapes
- `07–10` — delete/clear operations

### `examples/01_virtuoso/maestro/`
- `01_read_focused_maestro.py` — in-memory snapshot of the focused maestro (config + env + results + outputs + corners + variables)
- `03_bg_open_read_close_maestro.py` — background open → read config → close
- `06a_rc_create.py` — create RC schematic + Maestro setup
- `06b_rc_simulate.py` — run simulation
- `06c_rc_read_results.py` — read results, export waveforms, open GUI
- `07_gui_session_lifecycle.py` — GUI session lifecycle integration test (open/close edge cases)
- `08_gui_open_snapshot_close.py` — GUI open → snapshot artifacts → close (owns lifecycle)
- `09_snapshot_with_metrics.py` — snapshot the focused maestro to a timestamped directory (disk artifacts)

## Common workflows

### Find which library contains a cell

`ddGetObj(cellName)` with a single argument returns nil — must iterate `ddGetLibList()`:

```python
r = client.execute_skill(f'''
let((result)
  result = nil
  foreach(lib ddGetLibList()
    when(ddGetObj(lib~>name "{CELL}")
      result = cons(lib~>name result)))
  result)
''')
# r.output e.g. '("2025_FIA")'
```

No need for a separate script — inline in any workflow that needs to locate a cell before operating on it.

### Create a schematic

```python
from virtuoso_bridge.virtuoso.schematic import (
    schematic_create_inst_by_master_name as inst,
    schematic_create_pin as pin,
)

with client.schematic.edit(LIB, CELL) as sch:
    # 1. Place instances — sch.add() queues SKILL commands
    sch.add(inst("tsmcN28", "pch_mac", "symbol", "MP0", 0, 1.5, "R0"))
    sch.add(inst("tsmcN28", "nch_mac", "symbol", "MN0", 0, 0, "R0"))

    # 2. Label MOS terminals with stubs — NOT manual add_wire
    sch.add_net_label_to_transistor("MP0",
        drain_net="OUT", gate_net="IN", source_net="VDD", body_net="VDD")
    sch.add_net_label_to_transistor("MN0",
        drain_net="OUT", gate_net="IN", source_net="VSS", body_net="VSS")

    # 3. Add pins at circuit EDGE, not on terminals
    sch.add(pin("IN",  -1.0, 0.75, "R0", direction="input"))
    sch.add(pin("OUT", -1.0, 0.25, "R0", direction="output"))
    # schCheck + dbSave happen automatically on context exit
```

**Key rules:**
- **Use `add_net_label_to_transistor`** for MOS D/G/S/B — it auto-detects stub direction. Never manually `add_wire` between terminals.
- **Pins go at the circuit edge**, not on instance terminals. They connect via matching net names.
- **Delete before recreate** — if the cell already exists, `add_instance` accumulates on top of old instances:
  ```python
  client.execute_skill(f'ddDeleteObj(ddGetObj("{LIB}" "{CELL}"))')
  ```
- **CDF parameters** — two-step process:

  **Step 1: Set values** with `schHiReplace` (Edit > Replace). Do NOT use `param~>value =` or `dbSetq` — they don't update display or derived params.
  ```python
  client.execute_skill(
      'schHiReplace(?replaceAll t ?propName "cellName" ?condOp "==" '
      '?propValue "pch_mac" ?newPropName "w" ?newPropValue "500n")')
  ```

  **Step 2: Trigger CDF callbacks** with `CCSinvokeCdfCallbacks` to update derived parameters (finger_width, display annotations, etc.). Use `?order` to run only the changed params — running all callbacks may fail on PDK-specific variables like `mdlDir`.
  ```python
  # Must load CCSinvokeCdfCallbacks.il first (one-time)
  client.upload_file("reference/CCSinvokeCdfCallbacks.il", "/tmp/CCSinvokeCdfCallbacks.il")
  client.execute_skill('load("/tmp/CCSinvokeCdfCallbacks.il")')

  # Trigger only the callbacks you need
  client.execute_skill('CCSinvokeCdfCallbacks(geGetEditCellView() ?order list("fingers"))')
  ```

  **Critical:** PDK devices have `nf` as read-only. Use `fingers` instead:
  ```python
  # ✅ "fingers" is editable, "nf" is not
  client.execute_skill(
      'schHiReplace(?replaceAll t ?propName "cellName" ?condOp "==" '
      '?propValue "pch_mac" ?newPropName "fingers" ?newPropValue "4")')

  # ❌ schHiReplace(...?newPropName "nf" ...) → SCH-1725 "not editable"
  ```

  **Why two steps:** `schHiReplace` changes the stored property but does NOT trigger CDF callbacks. Without callbacks, derived params (finger_width, m_ov_nf annotations) stay stale. `CCSinvokeCdfCallbacks(?order ...)` triggers only the specified callbacks, avoiding PDK errors from unrelated callbacks.

  Or use the Python wrapper which handles both steps:
  ```python
  from virtuoso_bridge.virtuoso.schematic.params import set_instance_params
  set_instance_params(client, "MP0", w="500n", l="30n", nf="4", m="2")
  ```

### Read a design (schematic + maestro + netlist)

**Always use the Python API functions below. Do NOT hand-write SKILL for reading.**

```python
from virtuoso_bridge import VirtuosoClient, decode_skill_output
client = VirtuosoClient.from_env()
LIB, CELL = "myLib", "myCell"

# 1. Schematic — default: topology only (no positions/geometry)
from virtuoso_bridge.virtuoso.schematic.reader import read_schematic
data = read_schematic(client, LIB, CELL, include_positions=False)
# data = {
#     "instances": [{"name", "lib", "cell", "numInst", "view",
#                    "params": {...}, "terms": {...}}, ...],
#     "nets": {"VN1": {"connections": ["M0.D", ...], "numBits": 1,
#                       "sigType": "signal", "isGlobal": false}, ...},
#     "pins": {"VINP": {"direction": "input", "numBits": 1}, ...},
#     "notes": [{"text": "...", ...}, ...]
# }

# With positions (only when you need xy/bBox, e.g. for layout-aware editing):
data_with_pos = read_schematic(client, LIB, CELL, include_positions=True)

# No CDF param filtering (return all 200+ PDK params):
raw = read_schematic(client, LIB, CELL, include_positions=False, param_filters=None)

# 2. Maestro — snapshot the focused window
#
# PREFER THE CLI for one-shot captures.  The CLI handles venv + client
# construction, and on-disk output is everything you need for
# analysis (state XMLs, SKILL probe text, per-point psf/* results,
# .rdb, netlist/).  Python for this case is pure boilerplate.
#
#   $ virtuoso-bridge snapshot -o output/
#
# Use the Python API only when snapshot is one step in a larger
# same-connection pipeline (e.g. open_session → snapshot →
# run_simulation → close_session, or a loop over many cells):

from virtuoso_bridge.virtuoso.maestro import snapshot
d = snapshot(client)                             # SKILL-only, ~150ms, 1 round-trip
# d["raw_sections"] = [(probe_skill_text, raw_output), ...]
#   Each label IS the actual SKILL string we ran (e.g.
#   'maeGetAnalysis("test" "ac" ?session "fnxSession18")');
#   value is the verbatim SKILL alist — no Python parsing.
# d also has session / lib / cell / view / mode / unsaved.

# Full disk dump (raw + YAML-filtered XMLs + 16 SKILL probes + per-point
# inputs + spectre results + .rdb):
d = snapshot(client, output_root="output/")      # → d["output_dir"]

# IMPORTANT: snapshot() always uses the CURRENTLY FOCUSED maestro window.
# Click the desired ADE Assembler first, or use open_session() to bring it up.

# Rule of thumb: one-shot inspection → CLI; multi-step pipeline → Python.

# 3. Netlist — generate from maestro session, download via SSH
session = open_session(client, LIB, CELL)
test = decode_skill_output(
    client.execute_skill(f'car(maeGetSetup(?session "{session}"))').output)
client.execute_skill(
    f'maeCreateNetlistForCorner("{test}" "Nominal" "/tmp/nl_{CELL}" ?session "{session}")')
client.download_file(f"/tmp/nl_{CELL}/netlist/input.scs", "output/netlist.scs")
close_session(client, session)
```

### Run a simulation

**Follow this sequence exactly. Do not skip steps.**

```python
session = "fnxSession33"  # from find_open_session() or maeGetSessions()

# 1. Set variables
client.execute_skill(f'maeSetVar("CL" "1p" ?session "{session}")')

# 2. Save before running — REQUIRED, skipping causes stale state
client.execute_skill(
    f'maeSaveSetup(?lib "{LIB}" ?cell "{CELL}" ?view "maestro" ?session "{session}")')

# 3. Run (async — NEVER use ?waitUntilDone t, it deadlocks the event loop)
r = client.execute_skill(f'maeRunSimulation(?session "{session}")', timeout=30)
history = (r.output or "").strip('"')

# 4. Wait — blocks until simulation finishes (GUI mode only)
r = client.execute_skill("maeWaitUntilDone('All)", timeout=300)

# 5. Check for GUI dialog blockage — if wait returned empty/nil,
#    a dialog is blocking CIW. Try dismissing it:
if not r.output or r.output.strip() in ("", "nil"):
    client.execute_skill("hiFormDone(hiGetCurrentForm())", timeout=5)
    # If still stuck, user must manually dismiss the dialog in Virtuoso

# 6. Read results
# For per-point x per-output results across sweeps/corners -> use read_results
# (see references/simulation-flow.md). For ad-hoc single-output reads:
client.execute_skill(f'maeOpenResults(?history "{history}")', timeout=15)
r = client.execute_skill(f'maeGetOutputValue("myOutput" "myTest")', timeout=30)
value = float(r.output) if r.output else None
client.execute_skill("maeCloseResults()", timeout=10)
```

### Output read/export guardrails (collision-safe)

Apply these rules whenever you read or export **any** maestro output (scalar or waveform):

1. **History binding is mandatory**
    - Always use the exact `history` returned by `maeRunSimulation()`.
    - Pass that `history` explicitly to result readers/exporters (for example, `read_results(..., history=history)` and `export_waveform(..., history=history)`).
    - Do not rely on "latest" history inference when reproducibility matters.

2. **Remote filename must be unique per export**
    - Never use a fixed `/tmp/vb_wave_xxx.txt` path.
    - Use unique naming such as `/tmp/vb_wave_<history>_<timestamp>_<nonce>.txt`.
    - This avoids collisions with stale files from previous runs or other users.

3. **Bind results directory to the same history before ocnPrint**
    - After `maeOpenResults(?history ...)`, verify the resolved `resultsDir` contains `/<history>/`.
    - If mismatch is detected, stop and raise an error instead of exporting the wrong waveform.

**In optimization loops:** add `maeSaveSetup` and dialog-recovery in every iteration. GUI dialogs ("Specify history name", "No analyses enabled") block the entire SKILL channel — all subsequent `execute_skill` calls will timeout until the dialog is dismissed.

**Debug with screenshots:** if simulation appears stuck or results are unexpected, capture the Maestro window to see its current state:

```
client.execute_skill('''
hiWindowSaveImage(
    ?target hiGetCurrentWindow()
    ?path "/tmp/debug_maestro.png"
    ?format "png"
    ?toplevel t
)
''')
client.download_file("/tmp/debug_maestro.png", "output/debug_maestro.png")
```

This reveals dialog boxes, error messages, or unexpected variable values that are invisible through the SKILL channel alone.

### Root Cause: Why maeGetOutputValue returns nil for computed expressions

**Symptom:** `maeGetOutputValue("bandwidth(...)" testName)` returns nil, but `maeGetOutputValue("Noise_rms_out" testName)` returns a value.

**Root Cause:** The PSF directory contains no actual waveform data files. Check with:

```bash
# SSH to remote and check PSF directory
ssh zhangz@zhangz-wei "ls /server_local_ssd/.../Interactive.N/psf/<test>/psf/"
# Expected: .raw, simdata, spectre.log files
# Actual: only spectre.out, variables_file (NO waveform data!)
```

**Why this happens:**
- Maestro saves only **pre-computed scalar outputs** (like `Noise_rms_out`) to the RDB
- Raw waveforms (VOUT, VSIN signals) are NOT saved to PSF unless "save=all" is enabled
- Computed expressions (bandwidth, dB20, value) need the waveform data to calculate — returns nil

**Check the RDB directly:**

```bash
ssh zhangz@zhangz-wei "sqlite3 .../Interactive.N.rdb 'SELECT * FROM resultValue'"
# Returns rows like:
# 1|7|0.000469         -> Noise_rms_out scalar (saved)
# 1|8|wave             -> VF(/VOUT)/VF(/VSIN) is a waveform reference, not saved!
```

**Solution:** Enable "save all" option before running simulation:

```python
client.execute_skill(f'maeSetEnvOption("{test}" ?option "save" ?value "all")')
client.execute_skill('maeSaveSetup()')
```

### Reliable Result Reading: Parse the Log File

When Maestro OCEAN functions fail (due to missing PSF waveform data), parse the `.log` file:

```python
def read_maestro_results_from_log(client, LIB, CELL, history):
    """Read simulation results from the log file - most reliable method."""

    # Resolve the OA library path via SKILL — works on any setup,
    # no hardcoded ``/home/USER/...`` assumption.
    r = client.execute_skill(f'ddGetObj("{LIB}")~>readPath')
    lib_path = (r.output or "").strip().strip('"')
    log_path = f"{lib_path}/{CELL}/maestro/results/maestro/{history}.log"
    client.download_file(log_path, "/tmp/sim.log")
    
    # Parse tab-separated format: "expression\t\tvalue"
    results = {}
    with open("/tmp/sim.log") as f:
        for line in f:
            if "\t\t" in line:
                parts = line.rstrip().split("\t\t")
                if len(parts) >= 2:
                    name = parts[0].strip()
                    value = parts[1].strip()
                    # Skip header lines
                    if name and value and "corner" not in name.lower():
                        results[name] = value
    return results

# Full workflow:
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import open_gui_session, run_and_wait, close_gui_session

client = VirtuosoClient.from_env()
LIB, CELL = "PLAYGROUND_AMP", "TB_AMP_5T_D2S_DC_AC"

session = open_gui_session(client, LIB, CELL)  # GUI mode required for results
history, _ = run_and_wait(client, session=session, timeout=300)
h = history.strip('"')

results = read_maestro_results_from_log(client, LIB, CELL, h)
print(results)
# {'bandwidth(...)': '1.64M', 'dB20(...)': '10.93', ...}

close_gui_session(client, session, save=False)
```

**Log format in the file:**

```
bandwidth(abs((VF("/VOUT") / VF("/VSIN"))) 3 "low")		1.64M
dB20(value(abs((VF("/VOUT") / VF("/VSIN"))) 10000))		10.93
value(abs((VF("/VOUT") / VF("/VSIN"))) 10000)		3.519
Noise_rms_out						469u
```

### SKILL channel timeout — diagnosis and recovery

When `execute_skill()` times out, possible causes:

| Cause | Symptom | Fix |
|-------|---------|-----|
| **Modal dialog** | GUI popup blocking CIW | `virtuoso-bridge dismiss-dialog` |
| **Long operation** | Simulation or netlist running | Wait, or use `?waitUntilDone nil` |
| **CIW input prompt** | CIW waiting for typed input | `dismiss-dialog` (sends Enter) |
| **Bridge disconnected** | All calls fail immediately | `virtuoso-bridge restart` |

**Dialog recovery (bypasses SKILL, uses X11 directly):**

```bash
# Find and dismiss all blocking Virtuoso dialogs
virtuoso-bridge dismiss-dialog

# From Python
client.dismiss_dialog()
```

Uses `xwininfo` to find virtuoso-owned dialog windows and `XTestFakeKeyEvent` to send Enter. Works even when the SKILL channel is completely stuck.

**Prevention:** Always `dbSave(cv)` before `hiCloseWindow(win)`. Never use `?waitUntilDone t` in simulation calls. Add dialog-recovery in simulation loops (see "Run a simulation" section).

## Related skills

- **spectre** — standalone netlist-driven Spectre simulation (no Virtuoso GUI). Use when the user has a `.scs` netlist and wants to run it directly.

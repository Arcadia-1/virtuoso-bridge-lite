---
name: virtuoso
description: "MANDATORY — MUST load this skill when the user mentions: Virtuoso, Maestro, ADE, CIW, SKILL (Cadence), layout, schematic, cellview, OCEAN, design variables, or any Cadence EDA operation. Bridge to remote Cadence Virtuoso: SKILL execution, layout/schematic editing, ADE Maestro simulation setup via Python API. TRIGGER when the user mentions Virtuoso, SKILL (the Cadence language), Cadence IC, layout editing, schematic creation, cellview operations, CIW commands, ADE setup, Maestro configuration, design variables, OCEAN results, or any EDA task involving a Cadence design database — even if they just say 'draw a circuit' or 'place some transistors'."
---

# Virtuoso Skill

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

### Four domains

| Domain | What it does | Python package | API docs |
|--------|-------------|----------------|----------|
| **Schematic** | Create/edit schematics, wire instances, add pins | `client.schematic.*` | `references/schematic-python-api.md`, `references/schematic-skill-api.md` |
| **Layout** | Create/edit layout, add shapes/vias/instances | `client.layout.*` | `references/layout-python-api.md`, `references/layout-skill-api.md` |
| **Maestro** | Read/write ADE Assembler config, run simulations | `virtuoso_bridge.virtuoso.maestro` | `references/maestro-python-api.md`, `references/maestro-skill-api.md` |
| **General** | File transfer, screenshots, raw SKILL, .il loading | `client.*` | See below |

## Before you start

### Environment setup

> **Always use `uv` + virtual environment.** Never install into the global Python. `uv` refuses global installs by default, preventing accidental pollution.

```bash
uv venv .venv && source .venv/bin/activate   # Windows: source .venv/Scripts/activate
uv pip install -e .
```

All `virtuoso-bridge` CLI commands and Python scripts must run inside the activated venv.

### Connection sequence (follow in order)

1. **Check `.env`** — if the project has no `.env` yet, run **`virtuoso-bridge init`** to create one. If `.env` already exists, skip `init`.
2. **`virtuoso-bridge start`** — starts the local bridge service and SSH tunnel.
3. **If status is `degraded`** — the user must load the setup script in Virtuoso CIW (the `start` output tells them exactly what to run).
4. **`virtuoso-bridge status`** — verify everything is `healthy` before proceeding.

### Then

- **Check examples first**: `examples/01_virtuoso/` — don't reinvent from scratch.
- **Open the window**: `client.open_window(lib, cell, view="layout")` so the user sees what you're doing.

## Client basics

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()

client.execute_skill('...')                     # run SKILL expression
client.load_il("my_script.il")                  # upload + load .il file
client.upload_file(local_path, remote_path)      # local → remote
client.download_file(remote_path, local_path)    # remote → local
client.open_window(lib, cell, view="layout")     # open GUI window
client.run_shell_command("ls /tmp/")             # run shell on remote
```

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
> `python -c "..."` has shell 引号 + Python 引号 + SKILL 引号三层转义叠加，`\\n` 很容易变成 `\\\\n` 导致 `printf` 静默失败（不报错但不输出）。
> 正确做法：将代码写入 `.py` 文件再用 `python script.py` 执行，转义只有 Python + SKILL 两层，与例子一致。

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
| `references/maestro-python-api.md` | Session, read_config (verbose 0/1/2), writer functions |
| `references/netlist.md` | CDL/Spectre netlist formats, spiceIn import |

## Examples

**Always check these before writing new code.**

### `examples/01_virtuoso/basic/`
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
- `01_read_open_maestro.py` — read config from the currently open maestro
- `02_gui_open_read_close_maestro.py` — GUI open → read config → close
- `03_bg_open_read_close_maestro.py` — background open → read config → close
- `04_read_env.py` — read environment settings (model files, sim options, run mode)
- `05_read_results.py` — read simulation results (output values, specs, yield)
- `06a_rc_create.py` — create RC schematic + Maestro setup
- `06b_rc_simulate.py` — run simulation
- `06c_rc_read_results.py` — read results, export waveforms, open GUI

## Related skills

- **spectre** — standalone netlist-driven Spectre simulation (no Virtuoso GUI). Use when the user has a `.scs` netlist and wants to run it directly.

---
name: virtuoso
description: "Bridge to remote Cadence Virtuoso: SKILL execution, layout/schematic editing via Python API."
---

# Virtuoso Skill

## What you can do

Two approaches — use whichever fits:

### 1. Python API (preferred)

```python
from virtuoso_bridge import VirtuosoClient
client = VirtuosoClient.from_env()

# Execute any SKILL expression
client.execute_skill("1+2")
client.execute_skill('hiGetCurrentWindow()')

# Load a .il file into Virtuoso
client.load_il("path/to/script.il")

# Layout editing
with client.layout.edit("myLib", "myCell") as layout:
    layout.add_rect("M1", "drawing", (0, 0, 1, 0.5))
    layout.add_path("M2", "drawing", [(0, 0), (1, 0)], width=0.1)
    layout.add_label("M1", "pin", (0.5, 0.25), "VDD")
    layout.add_instance("tsmcN28", "nch_ulvt_mac", (0, 0), "M0")
    layout.add_via("M1_M2", (0.5, 0.25))
    shapes = layout.get_shapes()

# Schematic editing
with client.schematic.edit("myLib", "myCell") as sch:
    sch.add_instance("analogLib", "vdc", (0, 0), "V0", params={"vdc": "0.9"})
    sch.add_instance("analogLib", "gnd", (0, -0.5), "GND0")
    sch.add_wire([(0, 0), (0, 0.5)])
    sch.add_pin("VDD", "inputOutput", (0, 1.0))

# Other operations
client.open_window("myLib", "myCell", view="layout")
client.get_current_design()
client.save_current_cellview()
client.close_current_cellview()
client.download_file(remote_path, local_path)
client.run_shell_command("ls /tmp/")
```

### 2. Raw SKILL (when no Python API exists)

Write SKILL directly for anything the Python API doesn't cover:

```python
# Inline SKILL
client.execute_skill('dbOpenCellViewByType("myLib" "myCell" "layout")')

# Or write a .il file and load it
client.load_il("my_custom_script.il")
# Then call functions defined in it
client.execute_skill('myCustomFunction("arg1" "arg2")')
```

For bulk operations (thousands of shapes), put the loop in a `.il` file rather than sending a giant SKILL string — keeps each request payload small while the heavy loop runs inside Virtuoso.

## Startup check

Before any live Virtuoso action:

```bash
virtuoso-bridge status
```

If not healthy: `virtuoso-bridge restart`. If it says to load `virtuoso_setup.il`, paste that command in Virtuoso CIW first.

## Guidelines

- **Prefer Python API over raw SKILL** when a method exists (`client.layout.*`, `client.schematic.*`)
- **Open the window** with `client.open_window(...)` so the user can see what you're doing
- **Large edits**: split into chunks, open first with `mode="w"`, append with `mode="a"`
- **Screenshot after layout work**: use `examples/01_virtuoso/basic/04_screenshot.py` pattern to verify visually

## ADE control (Maestro mae* API)

All functions below are native Cadence SKILL — no extra `.il` file needed.

```python
# List / get / set design variables
client.execute_skill('maeGetSetup(?typeName "globalVar")')
client.execute_skill('maeGetVar("VDD")')
client.execute_skill('maeSetVar("VDD" "0.85")')

# Trigger simulation (async — GUI stays responsive)
client.execute_skill('maeRunSimulation()')
client.execute_skill("maeWaitUntilDone('All)")

# Read results via OCEAN (built into CIW, no extra loading)
client.execute_skill('openResults("...")')
client.execute_skill('selectResults("ac")')
client.execute_skill('ocnPrint(v("/OUT") ?output "/tmp/out.txt")')
client.download_file('/tmp/out.txt', local_path)
```

### Maestro: create, run, and display results

```python
# Create maestro + AC analysis
ses = client.execute_skill(f'maeOpenSetup("{lib}" "{cell}" "maestro")').output.strip('"')
client.execute_skill(f'maeCreateTest("AC" ?lib "{lib}" ?cell "{cell}" ?view "schematic" ?simulator "spectre" ?session "{ses}")')
client.execute_skill(f'maeSetAnalysis("AC" "ac" ?enable t ?options `(("start" "1") ("stop" "10G") ("dec" "20")) ?session "{ses}")')
client.execute_skill(f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" ?view "maestro" ?session "{ses}")')

# Run simulation (async — GUI stays responsive)
client.execute_skill('maeRunSimulation()')
client.execute_skill("maeWaitUntilDone('All)")

# Open maestro GUI with latest history
# 1. Close old sessions (edit mode is exclusive)
# 2. List histories: getDirFiles on results dir, filter dot-prefixed
# 3. deOpenCellView → maeMakeEditable → maeRestoreHistory → maeSaveSetup
```

See `examples/01_virtuoso/ade/01_rc_filter_sweep.py` for the complete workflow.

## References

Load only when needed:

- `references/layout.md` — layout API details and SKILL examples
- `references/schematic.md` — schematic API details and examples
- `references/ade.md` — ADE control, simulation triggering, OCEAN result reading
- `references/netlist.md` — CDL/Spectre netlist formats, spiceIn import, netlist export

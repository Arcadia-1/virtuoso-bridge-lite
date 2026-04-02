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

## References

Load only when needed:

- `references/layout.md` — layout API details and SKILL examples
- `references/schematic.md` — schematic API details and examples

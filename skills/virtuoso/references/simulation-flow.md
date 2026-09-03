# Standard Simulation Flow (GUI Mode)

Complete flow from opening Maestro to reading results. Follow this order exactly.

> **Why GUI mode?** Background sessions (`open_session` / `maeOpenSetup`) can
> read/write config but cannot run simulations reliably: completion callbacks
> may not fire, and `close_session` cancels in-flight runs. GUI mode is required
> for the supported simulation flow.

## The standard flow

```python
from virtuoso_bridge import VirtuosoClient, decode_skill_output

client = VirtuosoClient.from_env()
LIB, CELL = "myLib", "myTestbench"

# ── Step 0: Purge stale cellviews from memory ────────────────────
# Prevents ASSEMBLER-8127 caused by internal edit locks from
# previously closed sessions.
client.maestro.purge_maestro_cellviews()

# ── Step 1: Open maestro (handles cleanup automatically) ─────────
# open_gui_session cleans background sessions, closes other cells'
# windows, and opens in editable mode.
session = client.maestro.open_gui_session(LIB, CELL)

# Or manually (if you need more control):
# client.execute_skill('foreach(s maeGetSessions() errset(maeCloseSession(?session s ?forceClose t)))')
# client.execute_skill(f'deOpenCellView("{LIB}" "{CELL}" "maestro" "maestro" nil "a")')

# ── Step 2: (Optional) Modify variables, outputs, etc. ──────────
# client.execute_skill(f'maeSetVar("CL" "1p" ?session "{session}")')

# ── Step 3: Save + run + wait ────────────────────────────────────
# save_setup persists changes; run_and_wait starts the simulation
# with a completion callback and polls via SSH.
# SKILL channel stays free during the wait.
client.maestro.save_setup(LIB, CELL, session=session)
history, status = client.maestro.run_and_wait(session=session, timeout=600)
history = history.strip('"')
print(f"Simulation {status}: {history}")

# ── Step 4: Read results ─────────────────────────────────────────
results = client.maestro.read_results(session, lib=LIB, cell=CELL, history=history)
for point in results.get("points", []):
    print(f"Point {point['point']}: {point.get('parameters', {})}")
    for name, info in point.get("outputs", {}).items():
        print(f"  {name}: {info.get('value', '')} {info.get('pass_fail', '')}")

# ── Step 5: (Optional) Export waveforms ──────────────────────────
# client.maestro.export_waveform(session, 'VT("/VOUT")', "output/vout.txt",
#                 analysis="tran", history=history)
```

## When you already have an open GUI session

If Maestro is already open and editable (for example, the user opened it
manually), skip the purge/open stages and resolve that window's session before
saving and running:

```python
# Find the existing session
session = decode_skill_output(
    client.execute_skill('car(maeGetSessions())').output)

# Save, run, wait, read — same path as above
client.maestro.save_setup(LIB, CELL, session=session)
history, status = client.maestro.run_and_wait(session=session, timeout=600)
history = history.strip('"')
results = client.maestro.read_results(session, lib=LIB, cell=CELL, history=history)
```

## How `run_and_wait` works

1. Defines a SKILL callback procedure that writes a marker file when simulation finishes
2. Calls `maeRunSimulation(?callback "proc_name")` — callback is registered atomically with the simulation start (no race condition)
3. Polls the marker file via SSH, or the local filesystem in local mode, without using the SKILL channel
4. Returns `(history, status)` when marker appears

`timeout` is one end-to-end budget shared by acceptance of the
`maeRunSimulation` request and completion polling. Remote poll commands and
sleep intervals are capped to the remaining deadline. After the run request is
accepted, the SKILL channel remains free during the wait, so callers can read
configuration or use out-of-band X11 recovery.

## Detecting Maestro session state

There is **no direct SKILL API** to query whether a Maestro session is read-only, editable, or has unsaved changes. The `axl*` and `mae*` APIs (e.g. `maeIsEditable`, `axlGetSetupMode`) all return `nil`.

The only reliable method is parsing the **window title** via `hiGetWindowName`:

```python
r = client.execute_skill('''
foreach(mapcar w hiGetWindowList()
  let((s name)
    s = car(errset(axlGetWindowSession(w)))
    name = hiGetWindowName(w)
    when(s list(s name))))
''')
```

| Title pattern | State |
|---------------|-------|
| `...Assembler Editing: LIB CELL maestro` | Editable, no unsaved changes |
| `...Assembler Editing: LIB CELL maestro*` | Editable, **has unsaved changes** (trailing `*`) |
| `...Assembler Reading: LIB CELL maestro` | Read-only |

Use this before calling `maeMakeEditable()` to avoid ASSEMBLER-8127 deadlock.

## Closing Maestro sessions

### GUI-opened sessions (`maeCloseSession` won't work)

Sessions opened via the Virtuoso GUI (File → Open) **cannot be closed** with `maeCloseSession` — it returns ASSEMBLER-8051. You must close the GUI window:

```python
# Save first if modified (check for trailing * in title)
client.execute_skill(f'maeSaveSetup(?lib "{LIB}" ?cell "{CELL}" ?view "maestro" ?session "{session}")')

# Close by finding the window with matching session
client.execute_skill(f'''
foreach(w hiGetWindowList()
  when(car(errset(axlGetWindowSession(w))) == "{session}"
    hiCloseWindow(w)))
''')
```

### Background sessions (`maeOpenSetup`)

These can be closed with `maeCloseSession`:

```python
client.execute_skill(f'maeCloseSession(?session "{session}" ?forceClose t)')
```

### Clean up all sessions

```python
# Close GUI windows first (saves modified ones)
client.execute_skill('''
foreach(w hiGetWindowList()
  let((s name)
    s = car(errset(axlGetWindowSession(w)))
    when(s
      name = hiGetWindowName(w)
      when(name && rexMatchp("\\*$" name)
        maeSaveSetup(?session s))
      hiCloseWindow(w))))
''')

# Then close any remaining background sessions
client.execute_skill('''
foreach(s maeGetSessions() maeCloseSession(?session s ?forceClose t))
''')
```

## Common pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| Not purging before open | ASSEMBLER-8127 from stale internal lock | `client.maestro.purge_maestro_cellviews()` before `open_gui_session` |
| Using `open_session` for simulation | `run_and_wait` hangs / returns immediately | Use `open_gui_session` (GUI mode), not `open_session` (background) |
| Skipping `save_setup` | Simulation uses stale parameters | Always save before running |
| `maeCloseResults` leaves Maestro read-only | Next `maeRunSimulation` fails | Use `open_gui_session` to re-establish editable mode |
| `maeCloseSession` on GUI-opened session | ASSEMBLER-8051: "opened from UI" | Use `close_gui_session` instead |
| `window:N` in multi-line SKILL | `unbound variable - window` | Use `foreach(w hiGetWindowList() ...)` to find windows by `w~>windowNum` |

## Optimization loops

For sweeping parameters and re-running simulation:

```python
for val in ["1p", "2p", "5p", "10p"]:
    client.execute_skill(f'maeSetVar("CL" "{val}" ?session "{session}")')
    client.maestro.save_setup(LIB, CELL, session=session)
    history, status = client.maestro.run_and_wait(session=session, timeout=600)
    history = history.strip('"')
    results = client.maestro.read_results(session, lib=LIB, cell=CELL, history=history)
    # ... process results ...
```

Add dialog recovery (`client.dismiss_dialog()`) in the loop if GUI dialogs may appear.

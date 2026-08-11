# Troubleshooting — Known Gotchas & Pitfalls

When something fails unexpectedly, search this file for keywords (error message, function name, symptom) before debugging from scratch.

---

## SKILL / CIW

### `csh()` returns `t`/`nil`, not command output
Never use `csh()` or `sh()` to verify files or read command output. They only return success/failure. Use `download_file` (SSH/SCP) for all remote file operations.

### `procedurep()` returns `nil` for compiled functions
Functions like `maeCreateNetlistForCorner` are compiled into .cxt — `procedurep()` returns nil even though they work. Test by calling with wrong args instead.

### `printf` inside `foreach` loses output to Python
`execute_skill()` returns the **value** of the SKILL expression, not whatever `printf` wrote to the CIW. So a loop that prints per iteration leaves Python holding only the loop's return value (often the input list, which prints as opaque `dd:0x…` handles):
```scheme
foreach(lyr ddGetLibList() printf("%s\n" lyr~>name))  ; CIW shows names, Python sees the list of dd objects
```
Three idiomatic save-the-output patterns:

1. **`sprintf(nil ...)` + `strcat`** — accumulate as a string return value:
   ```scheme
   let((buf)
     buf = ""
     foreach(lyr layers
       buf = strcat(buf sprintf(nil "%s\n" lyr)))
     buf)
   ```
2. **Return a structured list** — `cons + reverse`, then parse in Python:
   ```scheme
   let((out)
     out = nil
     foreach(lyr layers
       out = cons(list(lyr lyr~>foo lyr~>bar) out))
     reverse(out))
   ```
   Combine with `client.fetch()` when iterating over DFII objects.
3. **`outfile` + `download_file`** — keep the literal `printf` style, route to a file:
   ```python
   client.execute_skill('''
   let((p)
     p = outfile("/tmp/probe.log")
     foreach(lyr layers fprintf(p "%s\n" lyr))
     close(p))
   ''')
   client.download_file("/tmp/probe.log", "logs/probe.log")
   ```

Rule of thumb: pattern 2 for structured data, pattern 3 for ad-hoc multi-line diagnostics.

### `inst~>prop` returns nil for PDK devices
MOS transistor parameters (W, L, nf, fingers, m) are stored in CDF, not in schematic instance properties. Use `cdfGetInstCDF(inst)` to read them:
```scheme
let((cdf)
  cdf = cdfGetInstCDF(inst)
  printf("W=%s L=%s nf=%s\n" cdf~>w~>value cdf~>l~>value cdf~>nf~>value))
```
`inst~>prop` only works for non-CDF properties like user-added annotations.

---

## GUI Dialog Blocking

### `simInitEnvWithArgs` triggers a GUI dialog
If the run directory already exists, the dialog "Run Directory exists but has not been used in SE. Initialize?" blocks the CIW event loop — all subsequent `execute_skill` calls hang until the user clicks OK.

**Workaround:** use a fresh (unique) directory name each time, or avoid `simInitEnvWithArgs` in automated flows.

### Maestro dialogs block the SKILL channel
GUI dialogs ("Specify history name", "No analyses enabled", etc.) block the entire CIW event loop. All `execute_skill` calls will timeout until the dialog is dismissed.

**Detection:** if `maeWaitUntilDone` returns empty/nil, a dialog is likely blocking.

**Recovery:**
```python
client.execute_skill("hiFormDone(hiGetCurrentForm())", timeout=5)
```
If still stuck, the user must manually dismiss the dialog in Virtuoso. Take a screenshot to diagnose:
```python
client.execute_skill('hiWindowSaveImage(?target hiGetCurrentWindow() ?path "/tmp/debug.png" ?format "png" ?toplevel t)')
client.download_file("/tmp/debug.png", "output/debug.png")
```

### ASSEMBLER-8127: cellview already open in edit mode
`maeMakeEditable()` fails with a modal dialog when the same cellview is already open in editable mode in another session (e.g. `fnxSession21` has it open while you try from `fnxSession0`). This dialog **completely blocks** the SKILL channel — even `hiFormDone` cannot reach it.

**Never call `maeMakeEditable()` unconditionally.** It can deadlock the bridge.

**Recovery when stuck:** if the remote has no `python3` or `xdotool`, send Enter via Python 2.7 + ctypes directly on the Virtuoso display:
```bash
# Find the Virtuoso DISPLAY (check /proc/<pid>/environ)
DISPLAY=<virtuoso_display> python2.7 -c "
import ctypes, ctypes.util
xlib = ctypes.cdll.LoadLibrary(ctypes.util.find_library('X11'))
xtst = ctypes.cdll.LoadLibrary(ctypes.util.find_library('Xtst'))
dpy = xlib.XOpenDisplay(None)
kc = xlib.XKeysymToKeycode(dpy, 0xff0d)
xtst.XTestFakeKeyEvent(dpy, kc, True, 0)
xtst.XTestFakeKeyEvent(dpy, kc, False, 0)
xlib.XFlush(dpy)
xlib.XCloseDisplay(dpy)
"
```

**Prevention:** before calling `maeMakeEditable()`, check if another session already has the cellview open in edit mode.

---

## Window / View Management

### `hiGetCurrentWindow()` returns the wrong window after open/close churn
After `hiCloseWindow` + `open_window` sequences (or any window-shuffling), the "current" window pointer can land on an unrelated panel — symptoms are `winName=nil`, `isLayoutMode=nil`, `viewBox=nil`. All subsequent `hiZoom` / `hiRedraw` calls then silently target the wrong window and the layout canvas stays empty.

**Diagnose:**
```scheme
let((w) w = hiGetCurrentWindow()
  sprintf(nil "wid=%L cv=%L lyt=%L"
          w (when(w~>cellView w~>cellView~>cellName)) w~>isLayoutMode))
```

**Fix — look up the window by content, drive it by wid explicitly:**
```python
WID = next(w["num"] for w in client.list_windows()
           if "MY_CELL" in (w.get("name") or "") and "layout" in (w.get("name") or ""))
client.execute_skill(f'hiZoom(window({WID}) list(0:0 700:700))')
client.execute_skill(f'hiRedraw(window({WID}))')
```
`window(N)` is the SKILL way to fetch a window by its integer ID — robust to focus changes. Never rely on `hiGetCurrentWindow()` for multi-step automation.

### `client.screenshot(target=...)` needs an `int`, not `str`
Targets are dispatched by Python type:
- `"ciw"` → CIW
- `"current"` → `hiGetCurrentWindow()` (see caveat above)
- `int` → window ID lookup via `windowNum`
- any other `str` → treated as a **view name** (`layout`/`schematic`/`maestro`)

`client.screenshot(target=str(wid))` ends up in the view-name branch and errors with `Window not found: <number>`. Always cast: `target=int(wid)`.

### Stale `~>cellView` handle after delete+recreate
If you `ddDeleteObj` a cell and then `dbOpenCellViewByType("w")` a new one with the same name, any pre-existing window still holds a pointer to the **old** cellview. Symptoms: layout canvas all black, `cv~>shapes` on the window's cv is empty, but `dbOpenCellViewByType(... "r")` from a fresh handle shows the correct shapes on disk.

**Fix:** close the stale window first, then re-open:
```python
for w in client.list_windows():
    nm = w.get("name") or ""
    if "MY_CELL" in nm and "layout" in nm:
        client.execute_skill(f'hiCloseWindow(window({w["num"]}))')
client.open_window(LIB, CELL, view="layout")
```
Closing drops the stale reference; the next `open_window` reads the new cellview from disk.

### `rexMatchp` errors on `nil` target abort the enclosing `let`
`hiGetWindowName(w)` returns `nil` for auxiliary panels (info dialogs, etc.). Passing `nil` to `rexMatchp` is a non-recoverable SKILL error inside a `let` — the entire block silently returns an empty string, no error surfaces in Python.

**Fix:** guard the predicate:
```scheme
foreach(w hiGetWindowList()
  let((nm) nm = hiGetWindowName(w)
    when(nm && rexMatchp("MY_CELL layout" nm) ...)))
```
Or skip SKILL string-matching entirely and filter on the Python side via `client.list_windows()` — much easier to debug.

---

## Netlist / si

### Netlist files are on the remote
`maeCreateNetlistForCorner` writes to the remote filesystem. Always use `client.download_file()` to retrieve them — don't try to read them via SKILL.

### si output location
`si -batch -command nl` outputs to `<runDir>/netlist` (a single file). But if something goes wrong (e.g. GUI dialog blocked), `spectre.inp` may be nearly empty. Check file size after download.

---

## Maestro / Design Variables

### `mae*` functions undefined (`*Error* undefined function`)
Older Virtuoso versions may not have `mae*` API. Use `asi*` equivalents instead. See the "asi\* Fallback" section in `maestro-skill-api.md` for the full mapping table. Detection: `fboundp('maeRunSimulation)`.

### `maeGetSetup(?typeName "globalVar")` reports an unsupported type
`"globalVar"` is not a valid `maeGetSetup` type in IC23.1 ISR16. Do not use it as a variable-discovery API. Use `asiGetDesignVarList(asiGetCurrentSession())` to read design variables; use `maeGetVar` / `maeSetVar` for one named variable. In IC23.1, `maeGetSetup` supports the types reported by its error message, including `"tests"`, `"corners"`, `"variables"`, and `"parameters"`.

### Global vs test-level variables
`maeSetVar("f" "1G")` sets a **global** variable. To set a test-level variable:
```python
client.execute_skill('maeSetVar("f" "1G" ?typeName "test" ?typeValue \'("IB_PSS"))')
```
If a test has a local variable with the same name, it overrides the global one. To delete test-level variables, use the `axl*` API (see main skill doc).

### Must `maeSaveSetup` before `maeRunSimulation`
Skipping save causes stale state — the simulation runs with old parameters. Always save before run.

---

## Connection / Tunnel

### Socket timeout at 30s
CIW is overloaded or a dialog is blocking. Check Virtuoso GUI state before retrying.

### `OPEN_FAILED` on view access
The cellview doesn't exist or is locked by another process. Verify with `ddGetObj(lib cell view)` before opening.

### `.il line 16` SKILL probe failure
The RAMIC daemon setup script failed to load. Re-run `load("/tmp/virtuoso_bridge_zhangz/setup.il")` in CIW.

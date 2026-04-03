# ADE Reference

## Supported ADE Types

| Type | Run function | Session access |
|------|-------------|----------------|
| **ADE Assembler (Maestro)** | `maeRunSimulation()` | `maeOpenSetup(lib cell "maestro")` |
| **ADE Explorer** | `sevRun(sevSession(win))` | `sevSession(win)` |

**Critical:** `sevRun` does not work for ADE Assembler — `sevSession()` returns nil on Assembler windows.

## Design Variables

```python
# List all global variables
client.execute_skill('maeGetSetup(?typeName "globalVar")')

# Get / Set
client.execute_skill('maeGetVar("VDD")')
client.execute_skill('maeSetVar("VDD" "0.85")')

# Parametric sweep (comma-separated)
client.execute_skill('maeSetVar("VDD" "0.8,0.9,1.0")')
```

## Maestro mae* API (IC618 / IC231)

All `mae*` functions operate on the **complete maestro cellview**, not just the visible window. If the maestro view is open in the GUI, `?session` can be omitted.

### Session Management

```scheme
; Open existing maestro (returns session string, e.g. "fnxSession4")
ses = maeOpenSetup("myLib" "myCell" "maestro")

; Open in append mode (for editing existing setup)
ses = maeOpenSetup("myLib" "myCell" "maestro" ?mode "a")
```

**`?session` is a string.** Pass it as `?session "fnxSession4"`, not as an unquoted variable.

### Creating Tests

```scheme
; Create a new test (session optional if maestro is open in GUI)
maeCreateTest("AC" ?lib "myLib" ?cell "myCell"
  ?view "schematic" ?simulator "spectre" ?session "fnxSession4")

; Copy from existing test
maeCreateTest("TRAN2" ?sourceTest "TRAN" ?session "fnxSession4")
```

### Analysis Configuration

Options use **backtick-quoted** SKILL list syntax:

```scheme
; AC analysis
maeSetAnalysis("AC" "ac" ?enable t ?options
  `(("start" "1") ("stop" "10G") ("incrType" "Logarithmic")
    ("stepTypeLog" "Points Per Decade") ("dec" "20")))

; Transient
maeSetAnalysis("TRAN" "tran" ?enable t ?options
  `(("stop" "60n") ("errpreset" "conservative")))

; DC operating point
maeSetAnalysis("TRAN" "dc" ?enable t ?options `(("saveOppoint" t)))

; Disable an analysis
maeSetAnalysis("AC" "tran" ?enable nil)

; Inspect analysis setup
maeGetAnalysis("AC" "ac")
; => (("anaName" "ac") ("sweep" "Frequency") ("start" "1") ("stop" "10G") ...)
```

### Outputs

```scheme
; Signal output (waveform)
maeAddOutput("OutPlot" "TRAN" ?outputType "net" ?signalName "/OUT")

; Expression output (scalar)
maeAddOutput("maxOut" "TRAN" ?outputType "point" ?expr "ymax(VT(\"/OUT\"))")

; Add spec (pass/fail check)
maeSetSpec("maxOut" "TRAN" ?lt "400m")   ; < 400mV
; Spec operators: ?lt (<), ?gt (>), ?minimum, ?maximum, ?tolerence
```

### Design Variables

```scheme
; Set global variable
maeSetVar("vdd" "1.3")
maeSetVar("vdd" "1.3" ?session "fnxSession4")

; Get global variable
maeGetVar("vdd")    ; => "1.3"

; Parametric sweep — comma-separated values
maeSetVar("c_val" "1p,100f" ?session "fnxSession4")
```

### Corners

```scheme
; Create corner and disable it for specific tests
maeSetCorner("myCorner" ?disableTests `("AC" "TRAN"))

; Set per-corner variable values (space-separated)
maeSetVar("vdd" "1.2 1.4" ?typeName "corner" ?typeValue '("myCorner"))
maeSetVar("temperature" "50 100" ?typeName "corner" ?typeValue '("myCorner"))
```

### Environment Options (Model Files)

```scheme
; Get current env options
maeGetEnvOption("TRAN")
maeGetEnvOption("TRAN" ?option "modelFiles")

; Set model files
maeSetEnvOption("TRAN" ?options
  `(("modelFiles" (("/path/to/model.scs" "tt")))))
```

### Save Setup

```scheme
maeSaveSetup(?lib "myLib" ?cell "myCell" ?view "maestro" ?session "fnxSession4")
```

### Running Simulation

```scheme
; Async — returns immediately with "Interactive.N"
; GUI stays responsive, results appear automatically in Maestro window
maeRunSimulation()
maeRunSimulation(?session "fnxSession4")

; Wait separately (if async)
maeWaitUntilDone('All)
```

**Important:** `maeRunSimulation(?waitUntilDone t)` blocks Virtuoso's event loop, which prevents the GUI from refreshing and can break the bridge connection. Use **async** `maeRunSimulation()` + `maeWaitUntilDone('All)` instead.

**Important:** Results only appear automatically in the Maestro GUI when the maestro window was opened via `deOpenCellView` **before** running. If maestro was only opened as a backend session (`maeOpenSetup`), results won't display.

### Reading Results (Programmatic)

```scheme
; Open specific history run (sets result pointer for programmatic access)
maeOpenResults(?history "Interactive.2")

; Query results
maeGetResultTests()                    ; => ("AC" "TRAN")
maeGetResultOutputs(?testName "AC")    ; => ("Vout")

; Get output value for a specific corner
maeGetOutputValue("maxOut" "TRAN2" ?cornerName "myCorner_2")
; => 0.6259399

; Check spec status
maeGetSpecStatus("maxOut" "TRAN2")
; => "fail"

; Export all results to CSV
maeExportOutputView(?fileName "/tmp/results.csv" ?view "Detail")

; Close results when done
maeCloseResults()
```

### Opening Maestro & Displaying History Results

To open a maestro view and display a previous simulation history:

```python
lib, cell = "myLib", "myCell"

# Step 1: Close all existing sessions (edit mode is exclusive)
r = client.execute_skill('maeGetSessions()')
for ses in r.output.strip('()').replace('"', '').split():
    if ses and ses != 'nil':
        client.execute_skill(f'maeCloseSession(?session "{ses}" ?forceClose t)')

# Step 2: List available histories via simulation results directory
#   Path: <simDir>/maestro/results/maestro/<historyName>/
#   Use getDirFiles to list, filter out dot-prefixed entries
r = client.execute_skill('asiGetResultsDir(asiGetCurrentSession())')
rd = r.output.strip('"')
base = re.match(r'(.*/maestro/results/maestro/)', rd).group(1)
r = client.execute_skill(f'getDirFiles("{base}")')
dirs = r.output.strip('()').replace('"', '').split()
histories = sorted([d for d in dirs if not d.startswith('.')])
latest = histories[-1]  # e.g. "Interactive.1"

# Step 3: Open GUI + make editable + restore history + save
client.execute_skill(f'deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "r")')
client.execute_skill('maeMakeEditable()')
client.execute_skill(f'maeRestoreHistory("{latest}")')
client.execute_skill(f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" ?view "maestro")')
```

Key points:
- **Edit mode is exclusive** — only one session can have a cellview in edit mode. Must close all existing sessions first via `maeCloseSession(?forceClose t)`.
- `deOpenCellView` opens the GUI window (read mode initially).
- `maeMakeEditable()` switches to edit mode (required before restoring).
- `maeRestoreHistory("Interactive.N")` sets the history as active setup, making results visible in the GUI.
- `maeSaveSetup` persists the state.
- History names are **not always** `Interactive.N` — they can be renamed by the user.

### Utility

```scheme
; Export entire setup as reproducible SKILL script
maeWriteScript("mySetupScript.il")

; Create standalone netlist for a specific corner
maeCreateNetlistForCorner("TRAN2" "myCorner_2" "./myNetlistDir")

; Migrate from ADE L / ADE XL to Maestro
maeMigrateADELStateToMaestro("myLib" "myCell" "spectre_state1")
maeMigrateADEXLToMaestro("myLib" "myCell" "adexl" ?maestroView "maestro_convert")
```

## CDF Parameter Setting

The Python schematic API doesn't support CDF parameters. Set them via SKILL after creating the schematic:

```python
# Open cellview for editing
client.execute_skill(f'_cv = dbOpenCellViewByType("{lib}" "{cell}" "schematic" nil "a")')

# Set a CDF parameter
client.execute_skill(
    'cdfFindParamByName(cdfGetInstCDF('
    'car(setof(i _cv~>instances i~>name == "R0")))'
    ' "r")~>value = "1k"'
)

client.execute_skill('dbSave(_cv)')
```

## Known Blockers

- **GUI dialogs** block the SKILL execution channel. All `execute_skill()` calls timeout until the dialog is dismissed manually. Common culprits: "Specify history name", "No analyses enabled".
- **Schematic must be checked & saved** before simulation, otherwise netlisting fails.
- **Schematic should be open in GUI** for Maestro to reference it correctly.

## Reading Results — OCEAN API

All OCEAN functions are built into CIW. No separate loading needed.

```python
results_dir = client.execute_skill(
    'asiGetResultsDir(asiGetCurrentSession())'
).output.strip('"')
client.execute_skill(f'openResults("{results_dir}")')
client.execute_skill('selectResults("ac")')
client.execute_skill('outputs()')
client.execute_skill('sweepNames()')

# Export waveform to text
client.execute_skill(
    'ocnPrint(dB20(mag(v("/OUT"))) ?numberNotation (quote scientific) '
    '?numSpaces 1 ?output "/tmp/ac_db.txt")'
)
client.download_file('/tmp/ac_db.txt', Path('output/ac_db.txt'))
```

## OCEAN Quick Reference

| Function | Purpose |
|----------|---------|
| `openResults(dir)` | Open PSF results directory |
| `selectResults(analysis)` | Select analysis type |
| `outputs()` | List available signal names |
| `sweepNames()` | List sweep variable names |
| `v(signal)` | Get voltage waveform object |
| `ocnPrint(wave ?output path)` | Export waveform to text file |
| `value(wave time)` | Get value at specific time |

## Complete Maestro Workflow (Python)

```python
client = VirtuosoClient.from_env()

# 1. Open schematic in GUI (required!)
client.open_window(lib, cell, view="schematic")

# 2. Open/create maestro
r = client.execute_skill(f'maeOpenSetup("{lib}" "{cell}" "maestro")')
ses = r.output.strip('"')

# 3. Create test + analysis
client.execute_skill(
    f'maeCreateTest("AC" ?lib "{lib}" ?cell "{cell}" '
    f'?view "schematic" ?simulator "spectre" ?session "{ses}")')
client.execute_skill(
    f'maeSetAnalysis("AC" "tran" ?enable nil ?session "{ses}")')
client.execute_skill(
    f'maeSetAnalysis("AC" "ac" ?enable t '
    f'?options `(("start" "1") ("stop" "10G") ("dec" "20")) '
    f'?session "{ses}")')

# 4. Add outputs + variables
client.execute_skill(
    f'maeAddOutput("Vout" "AC" ?outputType "net" '
    f'?signalName "/OUT" ?session "{ses}")')
client.execute_skill(f'maeSetVar("c_val" "1p,100f" ?session "{ses}")')

# 5. Save + run (synchronous)
client.execute_skill(
    f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" '
    f'?view "maestro" ?session "{ses}")')
r = client.execute_skill(
    f'maeRunSimulation(?waitUntilDone t ?session "{ses}")', timeout=300)

# 6. Export results
client.execute_skill(
    'maeExportOutputView(?fileName "/tmp/results.csv" ?view "Detail")')
client.download_file('/tmp/results.csv', 'output/results.csv')
```

## Examples

- `examples/01_virtuoso/ade/01_rc_filter_sweep.py` — complete Maestro workflow (create schematic, AC analysis, parametric sweep, read results, display in GUI)

# ADE Reference

## Supported ADE Types

| Type | Run function | Session access |
|------|-------------|----------------|
| **ADE Assembler (Maestro)** | `maeRunSimulation()` | `asiGetCurrentSession()` |
| **ADE Explorer** | `sevRun(sevSession(win))` | `sevSession(win)` |

**Critical:** `sevRun` does not work for ADE Assembler — `sevSession()` returns nil on Assembler windows. Check window title first to decide which run function to use.

## Setup

```python
client = VirtuosoClient.from_env()
client.load_il(Path('examples/01_virtuoso/assets/ade_bridge.il'))
```

## Design Variables

```python
# List all
client.execute_skill('adeBridgeListVars()')
# → (("VDD" "0.9") ("Vcm" "0.475"))

# Get / Set
client.execute_skill('adeBridgeGetVar("VDD")')
client.execute_skill('adeBridgeSetVar("VDD" "0.85")')

# Set sweep expression (start:step:stop)
client.execute_skill('adeBridgeSetVar("VDD" "0.81:0.09:0.99")')
```

## Triggering Simulation

```python
# ADE Assembler (Maestro)
r = client.execute_skill('maeRunSimulation()')
# Returns "Interactive.N" on success, async — returns immediately

# ADE Explorer
r = client.execute_skill('adeBridgeRunSim()')
```

## Maestro Session Management

| Function | Purpose |
|----------|---------|
| `maeOpenSetup(lib cell "maestro")` | Open maestro view directly (simpler than `deOpenCellView`) |
| `maeGetSetup(?lib l ?cell c ?view "maestro")` | Get complete setup details of a maestro cellview |
| `maeWaitUntilDone('All)` | Block until all simulations complete (critical — `maeRunSimulation` is async) |
| `maeExportOutputView(?fileName path ?view viewType)` | Export results to CSV |

### Synchronous Simulation Workflow

```scheme
maeOpenSetup("myLib" "myCell" "maestro")
maeRunSimulation()
maeWaitUntilDone('All)
maeExportOutputView(?fileName "/tmp/results.csv" ?view "Detail-Transpose")
```

### Headless Execution

Maestro scripts can run without GUI via `virtuoso -replay`:

```bash
# test.il:
# maeOpenSetup("myLib" "myCell" "maestro")
# maeRunSimulation()
# maeWaitUntilDone('All)
# maeExportOutputView()

virtuoso -replay test.il -log myLog1 -report
```

**Note:** `mae`-prefixed functions operate on the **complete cellview** (all tests), not just the currently visible ADE window.

## Known Blockers

- **"Specify history name" dialog**: blocks the SKILL execution channel. All subsequent `execute_skill()` calls timeout until the dialog is dismissed manually. Disable the prompt in ADE preferences before running via bridge.

## Reading Results — OCEAN API

All OCEAN functions are built into CIW. No separate loading needed.

```python
# 1. Open results
results_dir = client.execute_skill(
    'asiGetResultsDir(asiGetCurrentSession())'
).output.strip('"')
client.execute_skill(f'openResults("{results_dir}")')

# 2. Select analysis
client.execute_skill('selectResults("pss_td")')  # or "tran", "ac", "dc"

# 3. List signals and sweeps
client.execute_skill('outputs()')      # → ("/LP" "/LM" "/DCMPP" "/DCMPN")
client.execute_skill('sweepNames()')   # → ("VDD" "time")

# 4. Export waveform to text
client.execute_skill(
    'ocnPrint(v("/LP") ?numberNotation (quote scientific) '
    '?numSpaces 1 ?output "/tmp/lp.txt")'
)

# 5. Download
client.download_file('/tmp/lp.txt', Path('output/lp.txt'))
```

## ocnPrint Output Format

```
# Set No. 1

(VDD = 8.100e-01)
time (s)          v("/LP" ...) (V)
    0               810m
  214.68f           812.374m
  ...

# Set No. 2

(VDD = 9.000e-01)
...
```

Each `# Set No.` = one parametric sweep point. Parse with:
```python
sets = re.split(r'# Set No\. \d+\s*\n', text)[1:]
for s in sets:
    m = re.match(r'\(VDD = ([\d.eE+-]+)\)', s.strip())
    vdd = float(m.group(1))
```

## Complete Workflow

```python
client = VirtuosoClient.from_env()
client.load_il(Path('examples/01_virtuoso/assets/ade_bridge.il'))

# Set sweep → run → read
client.execute_skill('adeBridgeSetVar("VDD" "0.81:0.09:0.99")')
client.execute_skill('maeRunSimulation()')

# After sim completes, read results
results_dir = client.execute_skill(
    'asiGetResultsDir(asiGetCurrentSession())'
).output.strip('"')
client.execute_skill(f'openResults("{results_dir}")')
client.execute_skill('selectResults("pss_td")')

for sig in ['/LP', '/LM', '/DCMPP', '/DCMPN']:
    fname = sig.replace('/', '_').strip('_')
    client.execute_skill(
        f'ocnPrint(v("{sig}") ?numberNotation (quote scientific) '
        f'?numSpaces 1 ?output "/tmp/{fname}.txt")'
    )
    client.download_file(f'/tmp/{fname}.txt', Path(f'output/{fname}.txt'))

# Restore
client.execute_skill('adeBridgeSetVar("VDD" "0.9")')
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
| `asiGetCurrentSession()` | Get current ADE session |
| `asiGetResultsDir(sess)` | Get results directory path |
| `maeRunSimulation()` | Trigger Assembler simulation |

## Creating Maestro View from SKILL

A maestro view can be created programmatically without the GUI. Requires an open schematic cellview.

### Core API

| Function | Purpose |
|----------|---------|
| `deOpenCellView(lib cell "maestro" "maestro" nil "w")` | Create new maestro view (returns cellview, access session via `cv~>davSession`) |
| `deOpenCellView(lib cell "maestro" "maestro" nil "r")` | Open existing maestro view (read mode) |
| `maeCreateTest(name ?lib l ?cell c ?view "schematic" ?simulator "spectre" ?session ses)` | Add a test pointing to a schematic |
| `maeSetAnalysis(test analysis ?enable t ?options ...)` | Configure analysis (tran, dc, ac, etc.) |
| `maeSetEnvOption(test ?options ...)` | Set environment options (model files, etc.) |
| `maeAddOutput(name test ?outputType type ?signalName sig)` | Add waveform output (`outputType`: "net", "point") |
| `maeAddOutput(name test ?outputType "point" ?expr expr)` | Add expression output (e.g. `ymax(VT("/OUT"))`) |
| `maeSetVar(name value ?session ses)` | Set global design variable |
| `maeSetCorner(name ?session ses)` | Create a corner |
| `maeSetVar(name values ?typeName "corner" ?typeValue '("cornerName") ?session ses)` | Set per-corner variable (space-separated values) |
| `maeSaveSetup(?lib l ?cell c ?view "maestro" ?session ses)` | Save the maestro setup |
| `maeRunSimulation(?session ses)` | Run simulation |

### Corner Model File Setup

```scheme
x_mainSDB = axlGetMainSetupDB(session)
cornerHandle = axlGetCorner(x_mainSDB "cornerName")
modelHandle = axlPutModel(cornerHandle "modelName")
axlSetModelFile(modelHandle "/path/to/model.scs")
axlSetModelSection(modelHandle "tt")
```

### Check If Maestro View Already Exists

```scheme
member("maestro" dbAllCellViews(ddGetObj(libName) cellName))
```

### Analysis Options Examples

```scheme
; Transient
maeSetAnalysis("TRAN" "tran" ?enable t ?options `(("stop" "60n") ("errpreset" "conservative")))

; DC with save operating point
maeSetAnalysis("TRAN" "dc" ?enable t ?options '(("saveOppoint" t)))
```

### Full Example (SKILL)

```scheme
cv = geGetEditCellView()
libName = cv~>libName
cellName = cv~>cellName

maeCV = deOpenCellView(libName cellName "maestro" "maestro" nil "w")
ses1 = maeCV~>davSession

; Add test with tran + dc
maeCreateTest("TRAN" ?lib libName ?cell cellName ?view "schematic" ?simulator "spectre" ?session ses1)
maeSetAnalysis("TRAN" "tran" ?enable t ?options `(("stop" "60n") ("errpreset" "conservative")))
maeSetAnalysis("TRAN" "dc" ?enable t ?options '(("saveOppoint" t)))
maeSetEnvOption("TRAN" ?options '(("modelFiles" (("/path/to/model.scs" "tt")))))

; Add outputs
maeAddOutput("OutPlot" "TRAN" ?outputType "net" ?signalName "/OUT")
maeAddOutput("maxOut" "TRAN" ?outputType "point" ?expr "ymax(VT(\"/OUT\"))")

; Global variables
maeSetVar("vdc1" "1" ?session ses1)

; Corner
maeSetCorner("myCorner" ?session ses1)
maeSetVar("vdd" "1.2 1.4" ?typeName "corner" ?typeValue '("myCorner") ?session ses1)
maeSetVar("temperature" "50 100" ?typeName "corner" ?typeValue '("myCorner") ?session ses1)

; Corner model file
x_mainSDB = axlGetMainSetupDB(ses1)
cornerHandle = axlGetCorner(x_mainSDB "myCorner")
modelHandle = axlPutModel(cornerHandle "model.scs")
axlSetModelFile(modelHandle "/path/to/model.scs")
axlSetModelSection(modelHandle "fs")

; Save
maeSaveSetup(?lib libName ?cell cellName ?view "maestro" ?session ses1)
```

## Examples

- `examples/01_virtuoso/ade/01_list_design_vars.py`
- `examples/01_virtuoso/ade/02_get_set_var.py`
- `examples/01_virtuoso/ade/03_run_simulation.py`
- `examples/01_virtuoso/ade/04_create_maestro.py`

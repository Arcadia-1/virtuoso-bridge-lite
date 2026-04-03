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

## Examples

- `examples/01_virtuoso/ade/01_list_design_vars.py`
- `examples/01_virtuoso/ade/02_get_set_var.py`
- `examples/01_virtuoso/ade/03_run_simulation.py`

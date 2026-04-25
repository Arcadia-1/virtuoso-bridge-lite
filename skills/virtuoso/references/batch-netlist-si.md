# Batch Netlist (si)

Generate Spectre/HSPICE netlists without Maestro, using the `si` batch translator. Useful for automation and CI pipelines.

## Generate si.env from CIW

Don't write `si.env` manually -- let Virtuoso generate it:

```python
# Generate si.env on remote
client.execute_skill('sh("mkdir -p /tmp/si_run")')
client.execute_skill(
    'simInitEnvWithArgs("/tmp/si_run" "myLib" "myCell" "schematic" "spectre" nil)')

# Download to inspect or modify
client.download_file("/tmp/si_run/si.env", "output/si.env")
```

`simInitEnvWithArgs(runDir libName cellName viewName simulator nil)` -- the last arg is unused, pass nil.

## si.env fields

| Field | Meaning | Example |
|-------|---------|---------|
| `simLibName` | Library name | `"2025_FIA"` |
| `simCellName` | Cell name | `"_TB_INPUT_BUFFER_CASCODE_PSS"` |
| `simViewName` | View | `"schematic"` |
| `simSimulator` | Simulator type | `"spectre"` or `"hspice"` |
| `simViewList` | View search order for netlisting | `'("spectre cmos_sch schematic veriloga")` |
| `simStopList` | Stop descending at these views | `'("spectre")` |
| `simNetlistHier` | Hierarchical netlist | `t` |
| `nlDesignVarNameList` | Design variables to include | `'("VDD" "CL" "f")` |

## Run si batch netlist

```python
# Run si on remote via bridge (csh syntax -- use ; not &&)
client.run_shell_command(
    'mkdir -p /tmp/si_run ; '
    'cp /path/to/si.env /tmp/si_run/ ; '
    'cd /tmp/si_run ; '
    'si -batch -cdslib /home/zhangz/tsmc28/RISCA/cds.lib -command nl')

# Download the netlist
client.download_file("/tmp/si_run/netlist", "output/si_netlist.scs")
```

- For Spectre: use `-command nl` (NOT `netlist` -- that causes OSSHNL-510 errors)
- For HSPICE/auCdl/Verilog: use `-command netlist`
- `-cdslib` can be omitted if `cds.lib` exists in home directory
- `cds.lib` path can be found via: `client.execute_skill('simplifyFilename("./cds.lib")')`
- `run_shell_command` uses csh -- returns `t`/`nil`, not stdout. Don't rely on output.

Output file: `<runDir>/netlist` (a single file, not a directory).

## si vs Maestro netlist

| | si netlist | Maestro netlist (`maeCreateNetlistForCorner`) |
|---|---|---|
| **Circuit structure** | Yes | Yes (identical) |
| **parameters line** | No (variables stay symbolic) | Yes (resolved to values) |
| **model include** | No | Yes |
| **Simulation commands** | No | Yes (analysis, options) |
| **Requires Maestro** | No | Yes (open session) |

si gives a pure circuit netlist. Maestro gives a ready-to-run simulation deck.

## View netlist in Virtuoso GUI

```scheme
view("/tmp/si_run/netlist")
```

## From Maestro (alternative)

If a Maestro session is open, `maeCreateNetlistForCorner` is simpler:

```scheme
maeCreateNetlistForCorner("IB_PSS" "Nominal" "/tmp/netlist_dir")
; Output: /tmp/netlist_dir/netlist/input.scs

; View in GUI
view("/tmp/netlist_dir/netlist/input.scs")
```

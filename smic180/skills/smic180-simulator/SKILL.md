---
name: smic180-simulator
description: Build and run simulation testbenches for SMIC180 IO Ring or mixed-signal Cadence Virtuoso cells. Use when the user asks to create a testbench, export/redistribute a symbol, classify IO pins, place sources or loads, generate sim_config.json, run Spectre, sync Maestro setup, inspect simulation measurements, or continue a prior simulator run.
---

# SMIC180 IO Ring Simulator

Build a simulation testbench around an existing Virtuoso DUT cell and optionally run direct Spectre simulation with Maestro setup sync.

This skill is the simulation sibling of `SMIC180-ioring`. Use the generator to create the IO ring schematic/layout; use this simulator on the generated or any existing schematic cell.

## Output Contract

Use one shared output root:

```bash
AMS_OUTPUT_ROOT="${AMS_OUTPUT_ROOT:-<repo-root>/output}"
```

Simulator artifacts must go under:

```text
${AMS_OUTPUT_ROOT}/simulation/<YYYYMMDD_HHMMSS>/
${AMS_OUTPUT_ROOT}/simulation/.latest_run
```

## Entry Points

| Situation | Start here |
|---|---|
| Fresh run with `lib` and `cell` | Step 0 then Step 1 |
| `pin_info.json` exists but classifications are missing | Step 2 |
| `pin_classifications.json` and `sim_config.json` exist | Step 3 |
| Testbench exists and user only wants simulation | Step 4 |

## Analog Optimization V2

`scripts/analog_optimize.py` is the V2 entry point for general SMIC180 analog
circuit sizing. V2 uses a new, strict JSON format and is **not compatible** with
the legacy optimizer configuration or `sim_config.json`. Keep the legacy
testbench flow below for ordinary simulation; do not pass those files to the V2
commands.

```bash
python scripts/analog_optimize.py validate --config analog_opt_v2.json
python scripts/analog_optimize.py evaluate --config analog_opt_v2.json --candidate candidate.json
python scripts/analog_optimize.py run --config analog_opt_v2.json [--replace-work-cell] [--replace-result-cell]
python scripts/analog_optimize.py resume --run-dir output/analog_optimization/run
python scripts/analog_optimize.py report --run-dir output/analog_optimization/run
```

- `validate` is offline and checks the V2 schema without connecting to Virtuoso.
- `evaluate` evaluates one physical candidate JSON object. It safely creates the
  configured work cell before applying values. Candidate values are physical SI
  values keyed by parameter name, for example `{"M1_W": 1.0e-5}`; they are not
  normalized optimizer coordinates.
- `run` performs the configured search, fresh best-candidate replay, PVT
  validation, report generation, and conditional publication.
- `resume` reads `run_manifest.json` and
  `analog_opt_config.resolved.json` from an interrupted run directory.
- `report` regenerates `optimization_report.md` from
  `result_manifest.json` without a Virtuoso connection.

The source cell is never optimized in place. V2 creates a distinct `work_cell`
and publishes a distinct `result_cell`. Existing cells are protected by default;
use `run --replace-work-cell` and/or `--replace-result-cell` only when replacing
those exact configured cells is intentional.

### Parameters, stimuli, and PVT

- A stimulus is fixed unless it explicitly has `optimizable: true`, bounds, and
  a matching `target: "bias"` parameter. Fixed stimuli remain fixed during the
  search. PVT voltage substitution is a separate validation condition and uses
  `pvt.voltage_stimulus`.
- `target: "virtuoso_cdf"` covers MOS `W`, `L`, fingers, multiplicity,
  resistor, and capacitor CDF properties. Integer parameters such as fingers and
  `m` should use `dtype: "int"`. Use `sync_property` only when the PDK
  requires an explicitly synchronized property, such as MOS `w` and `fw`.
- `target: "spectre_variable"` optimizes a Spectre design variable through its
  `variable` name. This is separate from a source stimulus.
- `target: "bias"` optimizes a voltage or current stimulus that was explicitly
  marked optimizable.
- PVT corners, supply voltages, and temperatures validate the selected design;
  they are not search dimensions unless separately declared as parameters.

Every optimizable parameter has explicit `lower` and `upper` physical bounds.
Use `scale: "log"` only for positive ranges spanning multiplicative design
choices; use linear scaling for signed or narrow ranges. MOS `W/L`, R/C, and
bias values use SI internally even when a display `unit` is supplied. Spectre
variables are changed only when declared as `target: "spectre_variable"`; an
undeclared design variable is not an implicit optimization dimension.

Ordinary AC analysis reports gain, 3 dB bandwidth, and unity-gain frequency
when available. It does **not** report `phase_margin`; phase margin requires a
dedicated loop-stability/STB setup.

### Minimal validated configuration

This example optimizes one MOS width, holds VDD fixed during search, runs DC-op
and AC, enforces one gain constraint, uses seeded random search, and validates
TT at 3.3 V and 27 C. Replace library, cell, testbench, instance, and signal names
with names that exist in the target design.

```json
{
  "version": 2,
  "design": {
    "library": "tr",
    "cell": "amp",
    "work_cell": "amp_opt_work",
    "result_cell": "amp_opt_result",
    "testbench_cell": "amp_tb"
  },
  "stimuli": {
    "VDD": {
      "kind": "voltage",
      "value": "3.3V",
      "source_instance": "SRC_VDD"
    }
  },
  "parameters": [
    {
      "name": "M1_W",
      "target": "virtuoso_cdf",
      "instance": "M1",
      "property": "w",
      "sync_property": "fw",
      "unit": "um",
      "lower": 2e-6,
      "upper": 40e-6,
      "scale": "log"
    }
  ],
  "analyses": [
    {
      "name": "op",
      "type": "dc_op",
      "instances": ["M1"]
    },
    {
      "name": "ac_main",
      "type": "ac",
      "signal": "VOUT",
      "start": "10Hz",
      "stop": "100MHz",
      "points_per_decade": 20
    }
  ],
  "metrics": [],
  "specs": [
    {
      "metric": "ac.ac_main.gain_dc_db",
      "op": ">=",
      "value": 40.0,
      "hard": true
    }
  ],
  "search": {
    "method": "random",
    "evaluations": 12,
    "seed": 7
  },
  "pvt": {
    "corners": ["TT"],
    "voltages": ["3.3V"],
    "temperatures_c": [27],
    "voltage_stimulus": "VDD"
  },
  "outputs": {
    "run_dir": "output/analog_optimization/amp_v2"
  }
}
```

Typical run artifacts include `analog_opt_config.resolved.json`,
`run_manifest.json`, `workflow_state.json`, `search_history.json`, per-candidate
directories, `best_replay/`, `pvt/`, `pvt_results.json`,
`result_manifest.json`, and `optimization_report.md`. DC sweeps additionally
produce SVG curve artifacts. Resume uses the manifests and state in the same run
directory, skips completed work recorded by that run, and must not be pointed at
a partially copied artifact set. Do not move individual state files between
runs. Publication occurs only after fresh best replay and PVT validation pass;
otherwise the report remains available but the configured result cell is not
published.

## Step 0: Environment Setup

Auto-detect paths from this skill root. Do not hard-code an absolute install path.

```bash
SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SCRIPTS_PATH="${SKILL_ROOT}/scripts"
export PYTHONPATH="${SKILL_ROOT}:${PYTHONPATH:-}"

REPO_ROOT="$(cd "${SKILL_ROOT}" && while [ ! -f tools/SMIC180_config_export.py ] && [ "$(pwd)" != "/" ]; do cd ..; done; pwd)"
VENV_ROOT="$(cd "${SKILL_ROOT}" && while [ ! -d .venv ] && [ "$(pwd)" != "/" ]; do cd ..; done; pwd)"

if   [ -f "${VENV_ROOT}/.venv/Scripts/python.exe" ]; then export AMS_PYTHON="${VENV_ROOT}/.venv/Scripts/python.exe"
elif [ -f "${VENV_ROOT}/.venv/bin/python" ];         then export AMS_PYTHON="${VENV_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1;                then export AMS_PYTHON="python3"
elif command -v python  >/dev/null 2>&1;                then export AMS_PYTHON="python"
else echo "ERROR: No Python found."; return 1; fi

if [ -f "${REPO_ROOT}/tools/SMIC180_config_export.py" ]; then
  eval "$("$AMS_PYTHON" "${REPO_ROOT}/tools/SMIC180_config_export.py" --shell sh)"
fi

export VB_DISABLE_CONTROL_MASTER="${VB_DISABLE_CONTROL_MASTER:-1}"
```

All subsequent commands use `$AMS_PYTHON`.

Required simulator configuration:

- `_local/site.yaml` at the repository root.
- `~/.virtuoso-bridge/.env`, created by `virtuoso-bridge init`, for bridge connection values.

## Step 1: Symbol Export

Run when the user provides a Virtuoso library and cell with an existing schematic view.

```bash
$AMS_PYTHON "$SCRIPTS_PATH/symbol_export.py" <lib> <cell> [--vdd <vdd_value>]
```

Outputs:

```text
output/simulation/<timestamp>/pin_info.json
output/simulation/<timestamp>/dut_context.json
output/simulation/<timestamp>/build/
```

## Step 2: Pin Intent Authoring

Read the run directory from Step 1 output or `${AMS_OUTPUT_ROOT}/simulation/.latest_run`, then write two files:

1. `<run_dir>/pin_classifications.json`
2. `<run_dir>/sim_config.json`

Use `references/pin_classification.md` and `references/sim_config_rules.md`.

## Step 3: Testbench Build

```bash
$AMS_PYTHON "$SCRIPTS_PATH/tb_builder.py" --run-dir <run_dir>
```

## Step 4: Spectre Simulation

```bash
$AMS_PYTHON "$SCRIPTS_PATH/spectre_runner.py" --run-dir <run_dir>
```


## Step 4b: Multi-Corner Parallel Sweep

Run all SMIC180 process corners in parallel after baseline (tt) succeeds:

```bash
$AMS_PYTHON "$SCRIPTS_PATH/spectre_runner.py" --run-dir <run_dir> --corners
```

Or specify a subset:

```bash
$AMS_PYTHON "$SCRIPTS_PATH/spectre_runner.py" --run-dir <run_dir> --corners tt ff ss
```

Default SMIC180 corners: 	t, f, ss, nsp, snfp

**How it works:**
1. Baseline 	t Spectre runs first (must succeed)
2. patch_corner() modifies the deck: replaces core model section 	t → target corner, handles sub-corners (bjt_tt→bjt_ff, res_tt→res_ff, etc.), IO model sections untouched
3. All non-tt corners run in parallel via bridge-lite's SpectreSimulator.run_parallel()
4. Results written to <run_dir>/corner_results.json

**Output:**
```json
{
  "corners": {
    "tt": {"ok": true, "result_dir": "...", "measurements": {...}},
    "ff": {"ok": true, "result_dir": "...", "measurements": {...}},
    "ss": {"ok": false, "error": "spectre timeout"}
  }
}
```

## Step 5: Maestro Setup Sync

```bash
$AMS_PYTHON "$SCRIPTS_PATH/maestro_runner.py" --run-dir <run_dir>
```


## Step 6: Bayesian Optimization (Closed-Loop Tuning)

When simulation results do not meet specs, run the optimizer to automatically
tune stimulus parameters.  The optimizer uses a Gaussian Process surrogate
model with Expected Improvement acquisition — much more sample-efficient
than manual grid search or fixed decrement rules.

### 6a. Auto-generate optimizer config

`ash
 "/optimizer.py" --run-dir <run_dir> --generate-config
`

This scans pin_classifications.json for all tunable dc/pulse parameters
and writes optimization/optimizer_config.json.

### 6b. Edit optimizer config

Edit optimization/optimizer_config.json to:

1. **Set parameter bounds** (low/high) for each tunable parameter
2. **Set spec constraints** (spec_weights) — the objective minimizes
   total fractional violation across all pins:

   `json
   {
     "spec_weights": {
       "AVDD": {"p_max": "0.005"},
       "VIOLD": {"i_max": "0.05"},
       "EN":   {"vmax_above": "0.9*VDD", "vmin_below": "0.1*VDD"}
     }
   }
   `

3. **Set iteration budget** (max_iterations, default 30)

### 6c. Run optimization

`ash
 "/optimizer.py" --run-dir <run_dir> \
    --config optimization/optimizer_config.json
`

Or override iteration count:

`ash
 "/optimizer.py" --run-dir <run_dir> \
    --config optimization/optimizer_config.json --max-iter 20
`


Override batch size for parallel evaluations per iteration:

```bash
$AMS_PYTHON "$SCRIPTS_PATH/optimizer.py" --run-dir <run_dir> \
    --config optimization/optimizer_config.json --batch-size 4
```

Default batch-size is 1 (sequential). Higher values run multiple candidate
parameter sets in parallel per GP iteration — useful when Spectre licenses
are plentiful.

### 6d. Read results

| File | Content |
|------|---------|
| optimization/best_result.json | Best parameters, final violation, iteration count |
| optimization/optimization_history.json | Full history: every evaluated point + objective |
| pin_classifications.json | Updated with best-found parameters |

### How it works

1. **Latin Hypercube Sampling** for 2 × n_params initial evaluations
2. Fit **Gaussian Process** (RBF kernel) on (params → violation) pairs
3. **Expected Improvement** acquisition picks next point (explore vs exploit)
4. Each iteration: modify pin_classifications.json → rebuild TB → run Spectre → read measurements.json
5. Stop when: all specs pass (iolation ≈ 0) or no improvement in 5 iterations

### Dry-run mode

`ash
 "/optimizer.py" --run-dir <run_dir> \
    --config optimization/optimizer_config.json --dry-run
`

Prints parameter values without running Spectre — useful for verifying config.
## Analog Circuit Workflow (OpAmp, Bandgap, LDO, etc.)

For analog-only circuits (not IO rings), the workflow is the same but with
different pin classifications:

1. **Step 2**: Use `analog_input`/`analog_output`/`analog_power`/`analog_ground`/`bias_current` device classes. See `references/pin_classification.md` Section 6 for examples.
2. **Step 2**: Add AC analysis in `sim_config.json` if gain/bandwidth/phase margin is needed.
3. **Step 3**: TB builder places `vdc` for analog_input, `cap` for analog_output, `idc` for bias_current.
4. **Step 4**: Spectre runs with AC sweep for small-signal analysis.

### Bias Current Pin (IBIAS)

Bias current pins MUST use `isource` (current source), not `vsource`.
The `pin_classifications.json` must specify:
```json
{"device_class": "bias_current", "stimulus": "isource", "stimulus_params": {"dc": "-10u"}}
```

### AC Source Syntax

Spectre vsource AC parameters:
- **Correct**: `vsource dc=0.9 mag=1 phase=0 type=dc`
- **Wrong**: `vsource dc=0.9 acmag=1` (acmag is NOT a valid parameter)
- **Wrong**: `vsource dc=0.9 ac=1` (ac is NOT a valid parameter)

## Headless CIW Limitations

When running via headless CIW daemon (TCP bridge), some SKILL functions behave differently:

| Function | Works? | Notes |
|---|---|---|
| `dbCreateInst` | Yes | Core instance placement |
| `dbReplaceProp` | Yes | CDF parameter setting |
| `schCreateWire` with explicit coords | Yes | Use `list(list(x1 y1) list(x2 y2))` format |
| `schCheck` / `dbSave` | Yes | Always call after modifications |
| `schematic_create_wire_between_instance_terms` | **No** | Needs `inst~>master~>terminals` which is nil in headless mode |
| `schCreateWireLabel` | **No** | Needs schematic editor window |
| `schCreatePin` | **No** | Needs schematic editor window |
| `setof` with complex expressions | **Unreliable** | Use `foreach(inst cv~>instances when(...) ...)` instead |
| `open_cell_view(... mode="w")` | Creates empty | Use `mode="a"` to edit existing |

**Workaround**: Use `label_term_directed()` from `bridge/edit_patterns.py` instead of
`schematic_create_wire_between_instance_terms()`. The directed label function calculates
stub coordinates independently and uses `schCreateWire` with explicit points.

## Troubleshooting

| Problem | Fix |
|---|---|
| Virtuoso connection fails | `virtuoso-bridge status` -> `restart`; confirm daemon `.il` loaded in CIW |
| Spectre model missing | Check `spectre.io_model_include` and `spectre.core_model_include` in `_local/site.yaml` |
| Spectre license error | Set `spectre.lm_license_file` and `spectre.cds_lic_file` in `_local/site.yaml` |
| Library mapping mismatch | Verify `CDS_LIB_PATH_180` and `_local/site.yaml` cadence section |
| Netlist has no sources/loads | Ensure `pin_classifications.json` has correct device_class for all pins; check TB builder output for placed instances |
| AC analysis shows no gain | Verify one input has `acm: "1"` in stimulus_params; check `sim_config.json` has AC analysis enabled |
| `schematic_create_wire_between_instance_terms` fails | Use `label_term_directed()` instead; headless CIW cannot resolve master terminals |
| `setof` errors in headless mode | Use `foreach(inst cv~>instances when(...) ...)` pattern |
| VOUT=0V or unexpected DC operating point | Check bias current direction (negative for sinking); verify vdc values are non-round (e.g., 0.87 not 0.9) |
| Spectre "singular matrix" error | Add `gmin=1e-14` to sim_options; check for floating nodes; verify all pins connected |

## Verification Checklist

- [ ] Step 0: `$AMS_PYTHON` resolved and `_local/site.yaml` loaded.
- [ ] Step 1: `pin_info.json` exported.
- [ ] Step 2: `pin_classifications.json` and `sim_config.json` written.
- [ ] Step 3: `_tb` schematic created.
- [ ] Step 4: Spectre netlist generated and simulation executed.
- [ ] Step 5: Maestro setup synced (if applicable).
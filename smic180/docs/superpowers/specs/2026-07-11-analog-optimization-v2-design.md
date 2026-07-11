# SMIC180 Analog Optimization V2 Design

## 1. Objective

Build a new, non-backward-compatible analog optimization workflow under `smic180` using `virtuoso-bridge-lite` as the execution foundation. The workflow must optimize general SMIC180 analog circuits rather than encode LDO-specific behavior.

The first release covers:

- Real parameterized DC sweeps.
- MOS operating-point extraction.
- AC, noise, and transient metric extraction.
- Declarative specifications and normalized constraint violations.
- Optimization of MOS, passive, bias, and Spectre design-variable parameters.
- SMIC180 PVT validation.
- Safe creation and retention of optimized Virtuoso cell copies.

STB, Monte Carlo, PSRR/CMRR automation, distortion analysis, and layout/PEX optimization are intentionally deferred.

## 2. Design Principles

1. Never optimize the original Virtuoso cell directly.
2. Treat test stimuli, design parameters, and validation conditions as separate concepts.
3. Stimuli remain fixed unless explicitly marked `optimizable: true`.
4. Parse physical units once at the configuration boundary.
5. Use normalized `[0, 1]` coordinates only inside the search algorithm.
6. Convert normalized coordinates to physical values exactly once before evaluation.
7. Return finite penalties for failed simulations; never pass NaN or infinity to an optimizer.
8. Do not report phase margin from an ordinary input-to-output AC response.
9. Save an optimized cell only after a fresh best-point simulation and PVT validation.
10. Produce machine-readable results and a concise human-readable report for every run.

## 3. Architecture

Create a new package at:

```text
skills/smic180-simulator/analog_opt/
```

Modules and responsibilities:

```text
analog_opt/
├── __init__.py
├── schema.py          Configuration dataclasses, JSON loading, validation
├── units.py           Physical-unit parsing and formatting
├── parameters.py      Parameter definitions, normalization, quantization
├── apply.py           Virtuoso CDF and Spectre variable application
├── analyses.py        Analysis plan and Spectre deck configuration
├── metrics.py         DC/AC/noise/transient/op-point metric extraction
├── specs.py           Declarative constraints and normalized violations
├── evaluator.py       One candidate evaluation and artifact lifecycle
├── search.py          SciPy/TuRBO/random search adapters
├── pvt.py             SMIC180 corner/voltage/temperature validation
├── report.py          JSON and Markdown result generation
└── workflow.py        End-to-end orchestration
```

Add a new CLI entry point:

```text
skills/smic180-simulator/scripts/analog_optimize.py
```

Existing `scripts/optimizer.py` remains available for historical runs but is not used by V2.

## 4. Configuration Model

The new workflow reads `analog_opt_config.json`. Old `sim_config.json` and optimizer configuration files are not accepted.

Top-level sections:

```json
{
  "version": 2,
  "design": {},
  "stimuli": {},
  "parameters": [],
  "analyses": [],
  "metrics": [],
  "specs": [],
  "search": {},
  "pvt": {},
  "outputs": {}
}
```

### 4.1 Design

```json
{
  "design": {
    "library": "tr",
    "cell": "example_amp",
    "work_cell": "example_amp_opt_work",
    "result_cell": "example_amp_opt",
    "testbench_cell": "example_amp_opt_tb"
  }
}
```

The workflow deletes or replaces only the named work cell. It refuses to run when the work or result cell equals the source cell.

### 4.2 Stimuli

Stimuli are fixed by default:

```json
{
  "stimuli": {
    "VDD": {"kind": "voltage", "value": "3.3V"},
    "VIN": {"kind": "voltage", "dc": "1.2V", "ac": 1},
    "IBIAS": {"kind": "current", "value": "10uA"},
    "ILOAD": {"kind": "current", "value": "1mA"}
  }
}
```

A stimulus may participate in optimization only when it has `"optimizable": true` and valid bounds. This is intended for bias selection, not for silently changing verification conditions.

### 4.3 Parameters

Supported parameter targets:

- `virtuoso_cdf`: MOS W/L/fingers/m and PDK resistor/capacitor properties.
- `bias`: voltage or current source values.
- `spectre_variable`: design variables emitted as Spectre `parameters`.

Example:

```json
{
  "parameters": [
    {
      "name": "M1_W",
      "target": "virtuoso_cdf",
      "instance": "M1",
      "property": "w",
      "dtype": "float",
      "unit": "um",
      "lower": 2.0,
      "upper": 40.0,
      "scale": "log"
    },
    {
      "name": "M1_FINGERS",
      "target": "virtuoso_cdf",
      "instance": "M1",
      "property": "fingers",
      "dtype": "int",
      "lower": 1,
      "upper": 20,
      "step": 1
    },
    {
      "name": "CCOMP",
      "target": "spectre_variable",
      "variable": "CCOMP",
      "dtype": "float",
      "unit": "pF",
      "lower": 0.2,
      "upper": 20.0,
      "scale": "log"
    }
  ]
}
```

Integer parameters are rounded and clamped after denormalization. Stepped parameters are quantized to the nearest valid grid point.

## 5. Analysis Model

Supported first-release analyses:

- `dc_op`: one operating point.
- `dc_sweep`: a real Spectre parameter sweep.
- `ac`: logarithmic or linear frequency sweep.
- `noise`: input/output noise analysis with an explicit source and output.
- `tran`: transient analysis with fixed or parameterized stimuli.

A DC sweep must identify a Spectre parameter. The workflow parameterizes the referenced source or design variable before deck generation. A bare `dc dc` result is called `dc_op` and is never rendered as a sweep plot.

Example:

```json
{
  "analyses": [
    {"name": "op", "type": "dc_op"},
    {
      "name": "vdd_sweep",
      "type": "dc_sweep",
      "parameter": "VDD_SWEEP",
      "source": "VDD",
      "start": "2.7V",
      "stop": "3.6V",
      "points": 91
    },
    {
      "name": "ac_main",
      "type": "ac",
      "start": "1Hz",
      "stop": "1GHz",
      "points_per_decade": 100
    },
    {
      "name": "onoise",
      "type": "noise",
      "input_source": "VIN",
      "output": "VOUT",
      "start": "1Hz",
      "stop": "100MHz",
      "points_per_decade": 50
    },
    {
      "name": "step",
      "type": "tran",
      "stop": "20us",
      "max_step": "10ns",
      "errpreset": "conservative"
    }
  ]
}
```

## 6. Operating-Point Extraction

The workflow requests and parses device operating-point information for configured instances. Supported MOS fields initially include:

```text
id, gm, gds, vth, vgs, vds, vdsat, vbs, region, cgg, cgs, cgd
```

Derived metrics include:

```text
gm_over_id = abs(gm / id)
intrinsic_gain = abs(gm / gds)
saturation_margin = abs(vds) - abs(vdsat)
```

Missing fields are recorded as unavailable rather than replaced with zero. Specifications referencing unavailable fields fail validation before optimization.

## 7. Metric Extraction

Metrics use stable names independent of PSF file naming:

```text
dc.<analysis>.<signal>.value
dc.<analysis>.<signal>.min
dc.<analysis>.<signal>.max
dc.<analysis>.<signal>.slope
ac.<analysis>.gain_dc_db
ac.<analysis>.gain_peak_db
ac.<analysis>.bandwidth_3db_hz
ac.<analysis>.unity_gain_hz
noise.<analysis>.output_density_v_per_sqrt_hz
noise.<analysis>.integrated_output_vrms
tran.<analysis>.<signal>.overshoot
tran.<analysis>.<signal>.undershoot
tran.<analysis>.<signal>.settling_time_s
tran.<analysis>.<signal>.slew_rise_v_per_s
tran.<analysis>.<signal>.slew_fall_v_per_s
power.<supply>.average_w
current.<source>.average_a
op.<instance>.<field>
```

No `phase_margin` metric is emitted in V2 first release because a correct STB analysis is outside scope.

## 8. Declarative Specifications

Each specification references one metric and one comparison:

```json
{
  "specs": [
    {"metric": "ac.ac_main.gain_dc_db", "op": ">=", "value": 60, "weight": 2},
    {"metric": "ac.ac_main.bandwidth_3db_hz", "op": ">=", "value": "10MHz"},
    {"metric": "noise.onoise.integrated_output_vrms", "op": "<=", "value": "100uV"},
    {"metric": "power.VDD.average_w", "op": "<=", "value": "2mW"},
    {"metric": "op.M1.saturation_margin", "op": ">=", "value": "100mV", "hard": true}
  ]
}
```

Normalized violation definitions:

```text
For metric >= target: max(0, target - metric) / max(abs(target), epsilon)
For metric <= target: max(0, metric - target) / max(abs(target), epsilon)
For lower <= metric <= upper: distance outside range / max(abs(bound), epsilon)
```

The scalar search objective is the weighted sum of soft violations plus a configurable objective term. A hard-spec violation adds a large finite penalty. All-spec-pass means every violation is zero within tolerance.

## 9. Search and Scaling

The search layer receives only normalized vectors in `[0, 1]^N`.

The parameter layer owns all transformations:

```text
normalized -> physical/log physical -> quantized -> formatted CDF/Spectre value
```

Search algorithms:

- SciPy differential evolution as the default reliable backend.
- TuRBO when installed and requested for expensive searches with 3-20 parameters.
- Seeded random search as a dependency-free fallback.

The evaluator never rescales parameters. It accepts a fully materialized physical candidate dictionary. This boundary prevents the existing double-scaling defect.

## 10. Candidate Evaluation Lifecycle

For each candidate:

1. Materialize normalized coordinates into physical values.
2. Copy the source cell to the named work cell if the work cell is not initialized.
3. Apply all CDF parameter updates in one checked SKILL transaction.
4. Generate or patch Spectre design variables and fixed stimuli.
5. Export a fresh netlist.
6. Run configured analyses through `virtuoso-bridge-lite` Spectre APIs.
7. Parse operating point and waveform results.
8. Compute metrics and declarative violations.
9. Save candidate parameters, metrics, logs, and objective.
10. Return a finite scalar objective to the search backend.

A failed candidate records the failure category and receives a configurable finite penalty. Failure categories include configuration, CDF write, netlist, model, convergence, timeout, parse, and missing metric.

## 11. PVT Validation

PVT validation runs only for the fresh best candidate unless the configuration requests periodic validation.

SMIC180 process corners initially supported:

```text
tt, ff, ss, fnsp, snfp
```

Voltage and temperature lists are explicit configuration values. The validator evaluates the Cartesian product of process, supply, and temperature conditions. The result cell is saved only when every required hard specification passes at every required PVT point.

PVT output includes:

- Per-condition parameters and metrics.
- Worst value and worst condition for each specification.
- Pass/fail summary.
- Simulation failure summary.

## 12. Virtuoso Cell Safety

The workflow enforces these rules:

- Source cell is read-only from the workflow perspective.
- Work and result cell names must differ from the source.
- A pre-existing work cell may be replaced only with an explicit CLI flag.
- The result cell is written only after final validation.
- Original source parameters are never restored because they are never modified.
- Every CDF write is followed by `schCheck` and `dbSave`.
- The final netlist is inspected to confirm applied parameter values before result publication.

## 13. Artifacts

Each optimization run writes:

```text
output/analog_optimization/<timestamp>/
├── analog_opt_config.resolved.json
├── run_manifest.json
├── candidates/
│   └── <candidate_id>/
│       ├── parameters.json
│       ├── metrics.json
│       ├── specs.json
│       ├── spectre/
│       └── plots/
├── search_history.json
├── best_candidate.json
├── pvt_results.json
├── optimization_report.md
└── result_manifest.json
```

The report distinguishes measured facts, derived metrics, failed/unavailable metrics, and validation status.

## 14. CLI

Initial CLI:

```text
python scripts/analog_optimize.py validate --config <path>
python scripts/analog_optimize.py evaluate --config <path> --candidate <json>
python scripts/analog_optimize.py run --config <path>
python scripts/analog_optimize.py resume --run-dir <path>
python scripts/analog_optimize.py report --run-dir <path>
```

`validate` performs no Virtuoso edits or simulations. `evaluate` evaluates one candidate. `run` starts a search. `resume` continues from recorded history without repeating completed candidates.

## 15. Testing Strategy

Unit tests cover:

- Unit parsing and formatting.
- Linear/log normalization and exactly-once denormalization.
- Integer and stepped quantization.
- Configuration rejection for unsafe cell names and invalid bounds.
- DC-op versus DC-sweep deck generation.
- Metric extraction from representative PSF-like data.
- Declarative violation calculations.
- Finite penalties for failures.
- PVT Cartesian-product generation and worst-case reduction.

Integration tests use fake Virtuoso and Spectre adapters to verify lifecycle ordering and artifact creation. A live smoke test on a small SMIC180 circuit validates CDF application, netlist confirmation, one DC sweep, one AC run, and one PVT subset.

## 16. Acceptance Criteria

The first release is accepted when:

1. A new-format configuration can validate without accessing Virtuoso.
2. A real source voltage or design variable produces a multi-point DC sweep and visible SVG curve.
3. MOS operating-point fields and derived gm/Id and intrinsic gain are available for configured devices.
4. AC, noise, transient, current, and power metrics use stable names.
5. Declarative constraints produce correct normalized violations.
6. A two-parameter optimization cannot exhibit double scaling.
7. Fixed stimuli remain unchanged during optimization.
8. Candidate failures produce finite penalties and preserved logs.
9. The original Virtuoso cell is unchanged after successful and failed runs.
10. The final optimized cell is published only after fresh best-point and configured PVT validation.
11. Automated unit and integration tests pass.

## 17. Deferred Scope

The following require separate designs:

- Proper loop-gain insertion and Spectre STB analysis.
- Monte Carlo mismatch and yield optimization.
- Automated PSRR, CMRR, distortion, PSS, PAC, and Pnoise flows.
- Layout generation, DRC/LVS/PEX, and post-layout optimization.
- Multi-fidelity or surrogate models beyond TuRBO.
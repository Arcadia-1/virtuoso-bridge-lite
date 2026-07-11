# SMIC180 Analog Optimization V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new, non-backward-compatible SMIC180 analog optimization workflow with safe Virtuoso copies, real parameterized analyses, stable metrics, declarative constraints, exactly-once parameter scaling, search, and PVT validation.

**Architecture:** Add a focused `analog_opt` package under the simulator skill. Pure modules own configuration, units, parameter transforms, metrics, specifications, PVT planning, and reports; adapters isolate Virtuoso and Spectre side effects. The workflow evaluates physical candidate dictionaries and publishes a result cell only after a fresh best-point run and PVT pass.

**Tech Stack:** Python 3.12, dataclasses, JSON, pytest, NumPy, SciPy, optional TuRBO, Cadence SKILL through `VirtuosoClient`, Spectre through `virtuoso_bridge.spectre.SpectreSimulator`.

---

## File Map

Create production files under `smic180/skills/smic180-simulator/analog_opt/`:

`__init__.py`, `units.py`, `schema.py`, `parameters.py`, `specs.py`, `analyses.py`, `metrics.py`, `apply.py`, `evaluator.py`, `search.py`, `pvt.py`, `report.py`, and `workflow.py`.

Create CLI `smic180/skills/smic180-simulator/scripts/analog_optimize.py`.

Create tests under `smic180/tests/analog_opt/`. Do not modify or reuse `scripts/optimizer.py` for V2.

---

### Task 1: Test Harness and Physical Units

**Files:**
- Create: `smic180/tests/analog_opt/conftest.py`
- Create: `smic180/tests/analog_opt/test_units.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/__init__.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/units.py`

- [ ] **Step 1: Add the test import path**

`conftest.py` inserts `smic180/skills/smic180-simulator` into `sys.path`.

- [ ] **Step 2: Write failing unit tests**

```python
import pytest
from analog_opt.units import UnitError, format_quantity, parse_quantity

def test_parse_supported_units():
    assert parse_quantity("10uA", dimension="current") == pytest.approx(10e-6)
    assert parse_quantity("3.3V", dimension="voltage") == pytest.approx(3.3)
    assert parse_quantity("2pF", dimension="capacitance") == pytest.approx(2e-12)
    assert parse_quantity("10kOhm", dimension="resistance") == pytest.approx(10e3)

def test_reject_dimension_mismatch():
    with pytest.raises(UnitError, match="expected voltage"):
        parse_quantity("10uA", dimension="voltage")

def test_format_requested_unit():
    assert format_quantity(10e-6, "uA") == "10uA"
```

- [ ] **Step 3: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_units.py -v`

Expected: import failure because `analog_opt` does not exist.

- [ ] **Step 4: Implement strict parsing**

Implement `UnitError`, `parse_quantity(value, dimension)`, and `format_quantity(value_si, unit)`. Support scalar, V/mV/uV, A/mA/uA/nA, F/nF/pF, Ohm/kOhm/MOhm, Hz/kHz/MHz/GHz, s/ms/us/ns, m/mm/um/nm, and W/mW/uW. Reject unknown units and dimension mismatches.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest smic180/tests/analog_opt/test_units.py -v`

Expected: 3 passed.

- [ ] **Step 6: Commit**

`git commit -m "feat: add analog optimization unit handling"`

---

### Task 2: New Configuration Schema

**Files:**
- Create: `smic180/tests/analog_opt/test_schema.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/schema.py`

- [ ] **Step 1: Write failing schema tests**

```python
import json, pytest
from analog_opt.schema import ConfigError, load_config

def minimal_config():
    return {
        "version": 2,
        "design": {"library":"tr","cell":"amp","work_cell":"amp_opt_work","result_cell":"amp_opt","testbench_cell":"amp_opt_tb"},
        "stimuli": {"VDD":{"kind":"voltage","value":"3.3V"}},
        "parameters": [], "analyses": [{"name":"op","type":"dc_op"}],
        "metrics": [], "specs": [],
        "search": {"algorithm":"random","max_evals":5,"seed":7},
        "pvt": {"corners":["tt"],"voltages":["3.3V"],"temperatures_c":[27]},
        "outputs": {}
    }

def test_reject_old_version(tmp_path):
    data=minimal_config(); data["version"]=1
    p=tmp_path/"c.json"; p.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="version must be 2"): load_config(p)

def test_cells_must_be_distinct(tmp_path):
    data=minimal_config(); data["design"]["work_cell"]="amp"
    p=tmp_path/"c.json"; p.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="must be distinct"): load_config(p)

def test_fixed_stimulus_is_not_optimizable(tmp_path):
    p=tmp_path/"c.json"; p.write_text(json.dumps(minimal_config()))
    assert load_config(p).stimuli["VDD"].optimizable is False
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_schema.py -v`

Expected: missing `analog_opt.schema`.

- [ ] **Step 3: Implement schema dataclasses**

Create `ConfigError`, `DesignConfig`, `StimulusConfig`, `AnalogOptConfig`, and `load_config(path)`. Enforce version 2, distinct source/work/result cells, unique parameter and analysis names, bounds for optimizable stimuli, parameter targets limited to `virtuoso_cdf`, `bias`, and `spectre_variable`, and analyses limited to `dc_op`, `dc_sweep`, `ac`, `noise`, and `tran`.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest smic180/tests/analog_opt/test_schema.py -v`

Expected: 3 passed.

- [ ] **Step 5: Commit**

`git commit -m "feat: add analog optimization v2 schema"`

---

### Task 3: Exactly-Once Parameter Transforms

**Files:**
- Create: `smic180/tests/analog_opt/test_parameters.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/parameters.py`

- [ ] **Step 1: Write failing transform tests**

```python
import pytest
from analog_opt.parameters import ParameterSpace, ParameterSpec

def test_linear_denormalizes_once():
    s=ParameterSpace([ParameterSpec("M1_M","virtuoso_cdf",1,9,dtype="int")])
    assert s.materialize([0.5]) == {"M1_M":5}

def test_log_uses_log_domain():
    s=ParameterSpace([ParameterSpec("CCOMP","spectre_variable",1e-12,100e-12,scale="log")])
    assert s.materialize([0.5])["CCOMP"] == pytest.approx(10e-12)

def test_step_is_quantized_and_clamped():
    s=ParameterSpace([ParameterSpec("R1","virtuoso_cdf",1000,5000,step=500)])
    assert s.materialize([0.63])["R1"] == 3500
    assert s.materialize([1.2])["R1"] == 5000

def test_historical_double_scaling_regression():
    s=ParameterSpace([ParameterSpec("M7_M","virtuoso_cdf",1,40,dtype="int")])
    physical=s.materialize([0.5])
    assert physical["M7_M"] == 20
    assert s.materialize(s.normalize(physical))["M7_M"] == 20
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_parameters.py -v`

Expected: missing module.

- [ ] **Step 3: Implement transforms**

Create immutable `ParameterSpec` with name, target, lower, upper, dtype, scale, step, instance, property, variable, stimulus, and unit. `ParameterSpace.materialize()` clamps normalized coordinates, applies linear or logarithmic interpolation once, quantizes, rounds integers, and clamps physical bounds. `normalize()` performs the inverse only for resume/regression use.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest smic180/tests/analog_opt/test_parameters.py -v`

Expected: 4 passed.

- [ ] **Step 5: Commit**

`git commit -m "feat: add exactly-once parameter transforms"`

---

### Task 4: Declarative Specifications

**Files:**
- Create: `smic180/tests/analog_opt/test_specs.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/specs.py`

- [ ] **Step 1: Write failing tests**

```python
import math, pytest
from analog_opt.specs import Spec, evaluate_specs

def test_greater_equal_fractional_violation():
    r=evaluate_specs({"gain":48},[Spec("gain",">=",value=60,weight=2)])
    assert r.total == pytest.approx(0.4)

def test_less_equal_passes():
    r=evaluate_specs({"power":0.8e-3},[Spec("power","<=",value=1e-3)])
    assert r.total == 0 and r.passed

def test_missing_hard_metric_is_finite():
    r=evaluate_specs({},[Spec("op.M1.margin",">=",value=0.1,hard=True)],missing_penalty=1e5)
    assert math.isfinite(r.total) and r.total >= 1e5
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_specs.py -v`

- [ ] **Step 3: Implement `Spec`, `SpecResult`, `SpecSummary`, and `evaluate_specs()`**

Support `>=`, `<=`, and `between`. Use fractional violation from the approved design. Hard and missing failures add large finite penalties; never return NaN or infinity.

- [ ] **Step 4: Verify GREEN**

Expected: 3 passed.

- [ ] **Step 5: Commit**

`git commit -m "feat: add declarative analog specifications"`

---

### Task 5: Real Analysis Planning

**Files:**
- Create: `smic180/tests/analog_opt/test_analyses.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/analyses.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from analog_opt.analyses import AnalysisError, build_analysis_lines, is_curve_analysis

def test_dc_op_is_not_sweep():
    assert build_analysis_lines([{"name":"op","type":"dc_op"}]) == ["op dc"]
    assert not is_curve_analysis({"type":"dc_op"})

def test_real_dc_sweep():
    a={"name":"vdd","type":"dc_sweep","parameter":"VDD_SWEEP","source":"VDD","start":2.7,"stop":3.6,"points":91}
    assert build_analysis_lines([a]) == ["vdd dc param=VDD_SWEEP start=2.7 stop=3.6 lin=91"]
    assert is_curve_analysis(a)

def test_sweep_requires_two_points():
    with pytest.raises(AnalysisError,match="at least 2"):
        build_analysis_lines([{"name":"bad","type":"dc_sweep","parameter":"X","start":0,"stop":1,"points":1}])
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_analyses.py -v`

- [ ] **Step 3: Implement analysis generation**

Expose `build_analysis_lines()`, `required_source_parameters()`, and `is_curve_analysis()`. Generate valid lines for DC-op, DC sweep, AC, noise, and transient. A source sweep must map the named source to a Spectre parameter so the source is patched to `dc=<parameter>`.

- [ ] **Step 4: Verify GREEN**

Expected: 3 passed.

- [ ] **Step 5: Commit**

`git commit -m "feat: add real analog analysis planning"`

---

### Task 6: Stable Metrics and MOS Operating Points

**Files:**
- Create: `smic180/tests/analog_opt/test_metrics.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/metrics.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from analog_opt.metrics import extract_ac_metrics, extract_mos_op_metrics, extract_tran_metrics

def test_mos_derived_metrics():
    m=extract_mos_op_metrics("M1",{"id":10e-6,"gm":200e-6,"gds":2e-6,"vds":0.8,"vdsat":0.2})
    assert m["op.M1.gm_over_id"] == pytest.approx(20)
    assert m["op.M1.intrinsic_gain"] == pytest.approx(100)
    assert m["op.M1.saturation_margin"] == pytest.approx(0.6)

def test_ac_has_gain_but_no_fake_phase_margin():
    m=extract_ac_metrics("main",[1,10,1000],[100+0j,90+0j,1+0j])
    assert m["ac.main.gain_dc_db"] == pytest.approx(40)
    assert "ac.main.phase_margin" not in m

def test_transient_metrics():
    m=extract_tran_metrics("step","VOUT",[0,1e-6,2e-6,3e-6],[0,1.1,0.99,1.0],target=1.0)
    assert m["tran.step.VOUT.overshoot"] == pytest.approx(0.1)
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_metrics.py -v`

- [ ] **Step 3: Implement metric APIs**

Implement `extract_mos_op_metrics`, `extract_ac_metrics`, `extract_noise_metrics`, `extract_tran_metrics`, and `merge_metrics`. Omit unavailable fields rather than substituting zero. Integrate noise by trapezoidal integration of density squared. Emit stable names from the design spec and no phase-margin metric.

- [ ] **Step 4: Verify GREEN**

Expected: 3 passed.

- [ ] **Step 5: Commit**

`git commit -m "feat: add stable analog metric extraction"`

---

### Task 7: Safe Virtuoso Application

**Files:**
- Create: `smic180/tests/analog_opt/test_apply.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/apply.py`

- [ ] **Step 1: Write failing recording-client tests**

Test that two CDF updates are sent in one SKILL call, use `dbReplaceProp`, finish with `schCheck(cv)` and `dbSave(cv)`, and format 10 um as `10um`. Test that source and work cells cannot share a name.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_apply.py -v`

- [ ] **Step 3: Implement `VirtuosoApplier`**

Public methods:

`create_work_cell(library, source_cell, work_cell, replace)`, `apply_cdf(library, cell, specs, candidate)`, `read_cdf(...)`, and `publish_result_cell(...)`.

Use `dbCopyCellView`, group writes into one checked transaction, locate instances with `foreach`, update `fw` with `w` when needed, and raise `ApplyError` on bridge errors. Source cells are never opened in append mode.

- [ ] **Step 4: Verify GREEN**

Expected: all adapter tests pass.

- [ ] **Step 5: Commit**

`git commit -m "feat: add safe Virtuoso parameter application"`

---

### Task 8: Candidate Evaluator and Search

**Files:**
- Create: `smic180/tests/analog_opt/test_evaluator.py`
- Create: `smic180/tests/analog_opt/test_search.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/evaluator.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/search.py`

- [ ] **Step 1: Write evaluator tests**

Verify a physical candidate `{"M7_M":20}` reaches the backend unchanged and is saved unchanged. Verify convergence failure creates `failure.json` and returns a finite configured penalty.

- [ ] **Step 2: Write search tests**

Verify seeded random search produces the requested number of bounded physical candidates, stores `search_history.json`, chooses the lowest objective, and resume adds candidates without repeating completed IDs.

- [ ] **Step 3: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_evaluator.py smic180/tests/analog_opt/test_search.py -v`

- [ ] **Step 4: Implement evaluator boundary**

Create `EvaluationFailure`, `EvaluationResult`, and `CandidateEvaluator.evaluate(run_dir, candidate_id, physical_candidate)`. The evaluator must never import or call `ParameterSpace.materialize()`. Write candidate JSON artifacts atomically.

- [ ] **Step 5: Implement search adapters**

Create `SearchConfig`, `SearchResult`, and `run_search()`. Random uses `random.Random(seed)`; SciPy differential evolution receives only `(0,1)` bounds; optional TuRBO imports only when selected. Every backend calls `space.materialize()` exactly once per candidate.

- [ ] **Step 6: Verify GREEN**

Expected: evaluator and search tests pass.

- [ ] **Step 7: Commit**

`git commit -m "feat: add analog candidate evaluation and search"`

---

### Task 9: PVT and Reports

**Files:**
- Create: `smic180/tests/analog_opt/test_pvt.py`
- Create: `smic180/tests/analog_opt/test_report.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/pvt.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/report.py`

- [ ] **Step 1: Write PVT tests**

Verify 2 corners x 2 voltages x 2 temperatures creates 8 deterministic points. Verify a failed SS/low-voltage/high-temperature result is reported as the worst condition and blocks pass status.

- [ ] **Step 2: Write report tests**

Verify `optimization_report.md` separates measured, derived, and unavailable metrics. Verify `result_manifest.json.publishable` is true only when best-point specs and PVT both pass.

- [ ] **Step 3: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_pvt.py smic180/tests/analog_opt/test_report.py -v`

- [ ] **Step 4: Implement PVT model**

Create `PvtConfig`, `PvtPoint`, `PvtSummary`, `build_pvt_points()`, and `summarize_pvt()`. Accept only TT/FF/SS/FNSP/SNFP and preserve corner-major ordering.

- [ ] **Step 5: Implement reports**

Create `write_run_manifest()` and `write_report()`. Include best parameters, objective, specs, PVT worst cases, failures, and artifact paths. JSON output must be deterministic and UTF-8 without BOM.

- [ ] **Step 6: Verify GREEN**

Expected: all PVT and report tests pass.

- [ ] **Step 7: Commit**

`git commit -m "feat: add PVT validation and optimization reports"`

---

### Task 10: Live Backend, Workflow, and CLI

**Files:**
- Create: `smic180/tests/analog_opt/test_workflow.py`
- Create: `smic180/tests/analog_opt/test_cli.py`
- Create: `smic180/skills/smic180-simulator/analog_opt/workflow.py`
- Create: `smic180/skills/smic180-simulator/scripts/analog_optimize.py`

- [ ] **Step 1: Write workflow tests with fake adapters**

Verify order: create work cell, apply candidate, export fresh netlist, simulate, extract metrics, replay best, validate PVT, report, publish. Verify failed PVT does not call `publish_result_cell`.

- [ ] **Step 2: Write CLI offline validation test**

Use `subprocess.run` to call `analog_optimize.py validate --config <path>`. Assert return code 0 and no Virtuoso import or connection is required.

- [ ] **Step 3: Verify RED**

Run: `python -m pytest smic180/tests/analog_opt/test_workflow.py smic180/tests/analog_opt/test_cli.py -v`

- [ ] **Step 4: Implement `AnalogSimulationBackend`**

Partition physical values by target, apply CDF values, map bias and Spectre variables, preserve fixed stimuli, request a fresh testbench netlist, run through an injected `virtuoso-bridge-lite` runner, extract metrics, confirm final CDF/netlist values, and raise categorized finite failures.

- [ ] **Step 5: Implement `OptimizationWorkflow` state machine**

Persist transitions:

`validated -> work_cell_created -> searching -> best_replayed -> pvt_validated -> reported -> published`.

The best candidate must be freshly replayed. Publication occurs only after PVT passes.

- [ ] **Step 6: Implement CLI**

Support:

`validate --config PATH`
`evaluate --config PATH --candidate PATH`
`run --config PATH [--replace-work-cell] [--replace-result-cell]`
`resume --run-dir PATH`
`report --run-dir PATH`

Create live Virtuoso/Spectre adapters only for evaluate, run, and resume.

- [ ] **Step 7: Verify GREEN**

Expected: workflow and CLI tests pass.

- [ ] **Step 8: Commit**

`git commit -m "feat: add analog optimization workflow and CLI"`

---

### Task 11: Documentation and Offline Regression

**Files:**
- Modify: `smic180/skills/smic180-simulator/SKILL.md`
- Modify: `smic180/README.md`

- [ ] **Step 1: Document V2 commands and configuration**

Document validate, evaluate, run, resume, and report. Explain fixed stimuli versus optimizable parameters versus PVT conditions. State that old optimizer configurations are unsupported and ordinary AC does not provide phase margin.

- [ ] **Step 2: Add a complete minimal JSON example**

Use one MOS width, fixed VDD, DC-op and AC, one gain constraint, seeded random search, and TT/3.3 V/27 C PVT.

- [ ] **Step 3: Run full offline regression**

Run: `python -m pytest smic180/tests/analog_opt -v`

Expected: all tests pass without a VM connection.

- [ ] **Step 4: Run import and CLI checks**

Run:

`python -c "import sim_io; import analog_opt"`
`python smic180/skills/smic180-simulator/scripts/symbol_export.py --help`
`python smic180/skills/smic180-simulator/scripts/tb_builder.py --help`
`python smic180/skills/smic180-simulator/scripts/spectre_runner.py --help`
`python smic180/skills/smic180-simulator/scripts/analog_optimize.py --help`

Expected: exit code 0.

- [ ] **Step 5: Commit**

`git commit -m "docs: document SMIC180 analog optimization v2"`

---

### Task 12: Live SMIC180 Acceptance Smoke Test

**Files:**
- Runtime only: `smic180/output/analog_optimization/<timestamp>/`
- Do not commit simulation artifacts.

- [ ] **Step 1: Configure a small existing SMIC180 analog cell**

Use distinct source/work/result/testbench cells, one MOS width, fixed VDD, an 11-point VDD DC sweep, one AC analysis, TT/nominal/27 C, and two seeded random evaluations.

- [ ] **Step 2: Validate offline**

Run: `python smic180/skills/smic180-simulator/scripts/analog_optimize.py validate --config <path>`

Expected: valid with no Virtuoso edit.

- [ ] **Step 3: Evaluate one candidate**

Expected: work cell created, source unchanged, netlist contains exact candidate once, DC PSF has multiple samples, SVG contains a plotted path, and selected MOS has `gm_over_id`.

- [ ] **Step 4: Run two-point optimization and PVT**

Expected: bounded candidates, no double scaling, fresh best replay, PVT pass, result publication, and all required report files.

- [ ] **Step 5: Verify source immutability**

Read source CDF strings before and after. Expected: exact equality.

- [ ] **Step 6: Run final regression**

Run: `python -m pytest smic180/tests/analog_opt -v`

Expected: all tests pass.

- [ ] **Step 7: Correct any live defect test-first**

For each live defect, add a failing regression test, verify RED, implement the smallest correction, verify GREEN and full regression, then commit only affected files.

---

## Final Verification Checklist

- [ ] Version 2 is the only accepted optimization format.
- [ ] Fixed stimuli never change unless explicitly optimizable.
- [ ] Search operates only on normalized vectors.
- [ ] `ParameterSpace.materialize()` is the only normalized-to-physical conversion.
- [ ] Candidate evaluator never rescales physical values.
- [ ] DC operating points do not create sweep plots.
- [ ] Real DC sweeps create multi-point curves.
- [ ] MOS raw and derived operating-point metrics are available.
- [ ] AC metrics contain no fake phase margin.
- [ ] Noise and transient metrics are tested.
- [ ] Failures and missing metrics return finite penalties.
- [ ] PVT records worst conditions and blocks failed publication.
- [ ] Source cells remain unchanged after successful and failed runs.
- [ ] Result cells publish only after fresh replay and PVT pass.
- [ ] Offline tests and live smoke test pass.

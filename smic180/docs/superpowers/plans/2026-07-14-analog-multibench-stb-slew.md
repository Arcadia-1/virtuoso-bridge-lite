# SMIC180 Multi-Bench STB and Closed-Loop Slew Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add backward-compatible verification profiles, true Spectre STB margins, and standard closed-loop positive/negative slew through optimization, PVT, publication, final replay, and Maestro.

**Architecture:** Preserve the single-testbench V2 path and normalize it to one default profile. Apply each candidate once, then run independently exported profile testbenches with qualified metrics and aggregate hard-spec scoring. Live testbench creation remains evidence-driven and non-destructive.

**Tech Stack:** Python 3.9+, dataclasses, JSON, pytest, Virtuoso IC6.1.8, Spectre 18.1, PSF ASCII, SMIC180 PDK, Optimizer V2, Designer adapters.

---

## File Map

Create `analog_opt/profiles.py`, `stability.py`, `slew.py`, and `profile_testbenches.py`, with matching tests under `smic180/tests/analog_opt/`. Modify the existing schema, analysis, PSF, live adapter, workflow, PVT, report, final-validation, Maestro, CLI, and Designer adapter/report modules named in each task below.

### Task 1: Freeze the Existing Baseline

**Files:** `smic180/tests/analog_design/`, `smic180/tests/analog_opt/`, `smic180/tests/sim_io/`

- [ ] **Step 1: Run the complete regression**

```powershell
$py = D:\Codex_project\virtuoso_bridge\.venv\Scripts\python.exe
& $py -m pytest smic180/tests/analog_design smic180/tests/analog_opt smic180/tests/sim_io -ra
```

Expected before feature changes: `727 passed, 1 skipped`.

- [ ] **Step 2: Capture CLI and static baselines**

```powershell
& $py smic180/skills/smic180-simulator/scripts/analog_optimize.py --help
& $py -m compileall -q smic180/skills/smic180-analog-designer smic180/skills/smic180-simulator
git diff --check
```

Expected: exit `0`; existing CLI commands remain present.

- [ ] **Step 3: Preserve historical evidence**

Record only command, timestamp, and counts in the new Task 13 checkpoint. Do not modify historical live-run artifacts.

### Task 2: Add Backward-Compatible Verification Profile Schema

**Files:**
- Create: `smic180/skills/smic180-simulator/analog_opt/profiles.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/schema.py`
- Test: `smic180/tests/analog_opt/test_profiles.py`
- Test: `smic180/tests/analog_opt/test_schema.py`

- [ ] **Step 1: Write failing normalization test**

```python
def test_legacy_config_normalizes_to_default_profile(valid_config_path):
    config = load_config(valid_config_path)
    assert [p.id for p in config.verification_profiles] == [default]
    assert config.verification_profiles[0].testbench_cell == config.design.testbench_cell
```

- [ ] **Step 2: Run tests and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_profiles.py smic180/tests/analog_opt/test_schema.py -q`

Expected: FAIL because profile parsing and `verification_profiles` do not exist.

- [ ] **Step 3: Add the immutable profile model**

```python
@dataclass(frozen=True)
class VerificationProfileConfig:
    id: str
    role: str
    testbench_cell: str
    dut_instance: str
    stimuli: Mapping[str, Mapping[str, Any]]
    analyses: tuple[Mapping[str, Any], ...]
    metrics: tuple[Mapping[str, Any], ...]
    specs: tuple[Mapping[str, Any], ...]
    pvt_policy: str = full
    timeout_s: int = 1800
```

When profiles are absent, normalize legacy fields into `default`. Validate
unique IDs/cells, nonempty role and DUT, policy in
`nominal_only|selected|full`, positive timeout, unique analysis/metric names,
and profile spec references. Keep config version 2.

- [ ] **Step 4: Run compatibility tests**

Run: `& $py -m pytest smic180/tests/analog_opt/test_profiles.py smic180/tests/analog_opt/test_schema.py smic180/tests/analog_opt/test_cli.py -q`

Expected: PASS; old resolved JSON remains reloadable.

- [ ] **Step 5: Commit the profile schema.**

```powershell
git add smic180/skills/smic180-simulator/analog_opt/profiles.py smic180/skills/smic180-simulator/analog_opt/schema.py
git add smic180/tests/analog_opt/test_profiles.py smic180/tests/analog_opt/test_schema.py
git commit -m 'feat: add analog verification profiles'
```

### Task 3: Add Structured STB Analysis and PSF Loading

**Files:**
- Modify: `smic180/skills/smic180-simulator/analog_opt/analyses.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/live.py`
- Test: `smic180/tests/analog_opt/test_analyses.py`
- Test: `smic180/tests/analog_opt/test_live.py`

- [ ] **Step 1: Write failing STB rendering tests**

```python
def test_stb_renders_verified_spectre_form():
    analysis = {'name': 'loop', 'type': 'stb', 'probe': 'IPRB',
                'start': 1.0, 'stop': 1e9, 'points_per_decade': 50}
    assert build_analysis_lines([analysis]) == [
        'loop stb probe=IPRB start=1 stop=1000000000 dec=50'
    ]

def test_stb_requires_probe():
    with pytest.raises(AnalysisError, match='probe'):
        build_analysis_lines([{'name': 'loop', 'type': 'stb', 'start': 1,
                               'stop': 1e9, 'points_per_decade': 50}])
```

- [ ] **Step 2: Run tests and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_analyses.py smic180/tests/analog_opt/test_live.py -q`

Expected: FAIL with unsupported analysis type `stb` or absent result loading.

- [ ] **Step 3: Implement rendering and fresh complex-result lookup**

Extend `_ANALYSIS_TYPES` and `build_analysis_lines()` with the verified Spectre
18.1 syntax. Add `MetricsAdapter.load_complex_analysis()` that accepts explicit
result candidates, selects exactly one fresh finite complex trace, and rejects
missing, stale, duplicate, real-only, or non-finite data. Task 12 supplies the
live-discovered production result/trace names; no guessed names become defaults.

- [ ] **Step 4: Run focused tests**

Run: `& $py -m pytest smic180/tests/analog_opt/test_analyses.py smic180/tests/analog_opt/test_live.py -q`

Expected: PASS for STB rendering and all freshness failures.

- [ ] **Step 5: Commit**

```powershell
git add smic180/skills/smic180-simulator/analog_opt/analyses.py smic180/skills/smic180-simulator/analog_opt/live.py
git add smic180/tests/analog_opt/test_analyses.py smic180/tests/analog_opt/test_live.py
git commit -m 'feat: add structured Spectre STB analysis'
```

### Task 4: Extract True STB Stability Metrics

**Files:**
- Create: `smic180/skills/smic180-simulator/analog_opt/stability.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/live.py`
- Test: `smic180/tests/analog_opt/test_stability.py`

- [ ] **Step 1: Write failing crossing and margin tests**

```python
def test_stability_metrics_interpolate_unity_crossing():
    result = extract_stability_metrics('stability', 'loop', FREQ, LOOP_GAIN)
    assert result['stb.stability.loop.phase_margin_deg'] == pytest.approx(62.0)
    assert result['stb.stability.loop.unity_loop_frequency_hz'] == pytest.approx(2.2e7)

@pytest.mark.parametrize('response,error', [
    ([1+0j, 2+0j], 'unity crossing'),
    ([2+0j, .5+0j, 2+0j, .5+0j], 'ambiguous'),
])
def test_invalid_crossings_fail(response, error):
    with pytest.raises(StabilityError, match=error):
        extract_stability_metrics('stability', 'loop', range(len(response)), response)
```

- [ ] **Step 2: Run and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_stability.py -q`

Expected: FAIL because `analog_opt.stability` does not exist.

- [ ] **Step 3: Implement explicit loop-gain math**

Unwrap phase, interpolate crossings on log frequency, compute phase margin at
the selected 0 dB crossing, gain margin at the selected -180 degree crossing,
and emit low-frequency loop gain plus crossing evidence. Require a configured
crossing policy when multiple candidates exist. Undefined margins raise
`StabilityError`; ordinary AC data never calls this module.

- [ ] **Step 4: Run focused tests**

Run: `& $py -m pytest smic180/tests/analog_opt/test_stability.py smic180/tests/analog_opt/test_metrics.py smic180/tests/analog_opt/test_live.py -q`

Expected: PASS and existing AC metrics remain unchanged.

- [ ] **Step 5: Commit**

```powershell
git add smic180/skills/smic180-simulator/analog_opt/stability.py smic180/skills/smic180-simulator/analog_opt/live.py
git add smic180/tests/analog_opt/test_stability.py
git commit -m 'feat: measure true STB stability margins'
```

### Task 5: Extract Standard Closed-Loop Slew Metrics

**Files:**
- Create: `smic180/skills/smic180-simulator/analog_opt/slew.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/live.py`
- Test: `smic180/tests/analog_opt/test_slew.py`

- [ ] **Step 1: Write failing positive/negative fit tests**

```python
def test_closed_loop_slew_fits_twenty_to_eighty_percent():
    result = extract_closed_loop_slew('closed_loop_slew', 'step', 'VOUT', T, V,
                                      low=0.7, high=1.1, fractions=(0.2, 0.8))
    assert result.metrics['tran.closed_loop_slew.step.VOUT.slew_rise_v_per_s'] > 0
    assert result.metrics['tran.closed_loop_slew.step.VOUT.slew_fall_v_per_s'] > 0
    assert result.evidence['rise']['sample_count'] >= 3

def test_clipped_or_nonsettling_transition_fails():
    with pytest.raises(SlewError, match='settle|clipped'):
        extract_closed_loop_slew('closed_loop_slew', 'step', 'VOUT', T, CLIPPED,
                                  low=0.7, high=1.1, fractions=(0.2, 0.8))
```

- [ ] **Step 2: Run and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_slew.py -q`

Expected: FAIL because `analog_opt.slew` does not exist.

- [ ] **Step 3: Implement robust closed-loop extraction**

Fit output samples by least squares between configured 20 and 80 percent
thresholds. Report positive/negative slew, delays, settling, overshoot,
undershoot, final error, fit residual, interval, and sample count. Reject absent,
clipped, excessively non-monotonic, undersampled, or nonsettling transitions.
Dispatch only for `metric_mode: closed_loop_slew`; do not alter
`extract_tran_metrics()` or reinterpret historical open-loop traces.

- [ ] **Step 4: Run focused tests**

Run: `& $py -m pytest smic180/tests/analog_opt/test_slew.py smic180/tests/analog_opt/test_metrics.py smic180/tests/analog_opt/test_live.py -q`

Expected: PASS with both metric paths isolated.

- [ ] **Step 5: Commit**

```powershell
git add smic180/skills/smic180-simulator/analog_opt/slew.py smic180/skills/smic180-simulator/analog_opt/live.py
git add smic180/tests/analog_opt/test_slew.py
git commit -m 'feat: measure closed-loop slew rate'
```

### Task 6: Orchestrate Every Required Profile per Candidate

**Files:**
- Modify: `smic180/skills/smic180-simulator/analog_opt/profiles.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/workflow.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/live.py`
- Test: `smic180/tests/analog_opt/test_profiles.py`
- Test: `smic180/tests/analog_opt/test_workflow.py`

- [ ] **Step 1: Write failing aggregation and resume tests**

```python
def test_candidate_runs_profiles_after_one_cdf_apply(profile_backend):
    result = profile_backend(CANDIDATE, RUN_DIR)
    assert profile_backend.applier.apply_count == 1
    assert list(result['metadata']['profiles']) == ['open_loop', 'stability', 'closed_loop_slew']

def test_required_profile_failure_returns_finite_penalty(profile_backend):
    profile_backend.fail('stability', stage='metrics')
    result = profile_backend(CANDIDATE, RUN_DIR)
    assert math.isfinite(result['objective'])
    assert result['failure']['profile_id'] == 'stability'
```

- [ ] **Step 2: Run and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_profiles.py smic180/tests/analog_opt/test_workflow.py -q`

Expected: FAIL because the backend owns only one netlist/runner/metric adapter.

- [ ] **Step 3: Add profile-qualified execution**

Apply CDF once, then call a `ProfileRuntime` for every hard-spec profile in
stable ID order. Write each profile under `profiles/<id>/`; namespace metrics,
merge spec summaries, record per-profile netlist/result hashes and structured
failures, and return the existing evaluator protocol. Persist completed profile
IDs so resume starts at the first incomplete profile without reusing stale data.

- [ ] **Step 4: Run workflow regression**

Run: `& $py -m pytest smic180/tests/analog_opt/test_profiles.py smic180/tests/analog_opt/test_evaluator.py smic180/tests/analog_opt/test_workflow.py -q`

Expected: PASS; legacy default-profile evaluation preserves old paths and metrics.

- [ ] **Step 5: Commit**

```powershell
git add smic180/skills/smic180-simulator/analog_opt/profiles.py smic180/skills/smic180-simulator/analog_opt/workflow.py smic180/skills/smic180-simulator/analog_opt/live.py
git add smic180/tests/analog_opt/test_profiles.py smic180/tests/analog_opt/test_workflow.py
git commit -m 'feat: orchestrate multi-bench candidate evaluation'
```

### Task 7: Confirm Profile Stimuli, Probe, DUT, and Netlist

**Files:**
- Create: `smic180/skills/smic180-simulator/analog_opt/profile_testbenches.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/live.py`
- Test: `smic180/tests/analog_opt/test_profile_testbenches.py`
- Test: `smic180/tests/analog_opt/test_live.py`

- [ ] **Step 1: Write failing structural confirmation tests**

```python
def test_stability_confirmation_requires_oriented_iprobe():
    confirmation = confirm_profile_netlist(STB_PROFILE, EXPORTED_NETLIST)
    assert confirmation.probe == {'instance': 'IPRB', 'plus': 'VOUT', 'minus': 'VINN'}

def test_wrong_dut_master_or_pulse_is_rejected():
    with pytest.raises(ProfileTestbenchError, match='DUT master|pulse'):
        confirm_profile_netlist(SLEW_PROFILE, WRONG_NETLIST)
```

- [ ] **Step 2: Run and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_profile_testbenches.py smic180/tests/analog_opt/test_live.py -q`

Expected: FAIL because profile-aware netlist confirmation does not exist.

- [ ] **Step 3: Implement deterministic confirmation**

Parse the structured exported deck and prove DUT instance/master, explicit
sources, pulse values, load, analysis, save statements, `iprobe` instance and
orientation, and applied physical values. Copy prepared testbenches to unique
disposable cells and replace only the declared DUT master. Store source/disposable
cell names, hashes, `schCheck`, export time, and confirmation evidence.

- [ ] **Step 4: Run focused tests**

Run: `& $py -m pytest smic180/tests/analog_opt/test_profile_testbenches.py smic180/tests/analog_opt/test_live.py -q`

Expected: PASS for reordered netlists and FAIL for structural mismatches.

- [ ] **Step 5: Commit**

```powershell
git add smic180/skills/smic180-simulator/analog_opt/profile_testbenches.py smic180/skills/smic180-simulator/analog_opt/live.py
git add smic180/tests/analog_opt/test_profile_testbenches.py smic180/tests/analog_opt/test_live.py
git commit -m 'feat: confirm analog profile testbenches'
```

### Task 8: Gate Replay, PVT, Reports, and Publication with Profile Hashes

**Files:**
- Modify: `smic180/skills/smic180-simulator/analog_opt/pvt.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/workflow.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/report.py`
- Test: `smic180/tests/analog_opt/test_pvt.py`
- Test: `smic180/tests/analog_opt/test_report.py`
- Test: `smic180/tests/analog_opt/test_workflow.py`

- [ ] **Step 1: Write failing policy and publication tests**

```python
def test_full_profiles_run_at_every_pvt_point():
    matrix = build_profile_pvt_jobs(PROFILES, POINTS)
    assert len([j for j in matrix if j.profile_id == 'stability']) == len(POINTS)

def test_publication_requires_matching_profile_summary_hash(workflow):
    workflow.profile_summary_hash = 'changed'
    with pytest.raises(EvaluationFailure, match='profile summary hash'):
        workflow.publish()
```

- [ ] **Step 2: Run and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_pvt.py smic180/tests/analog_opt/test_report.py smic180/tests/analog_opt/test_workflow.py -q`

Expected: FAIL because PVT and publication are candidate-only.

- [ ] **Step 3: Implement profile-aware gates and artifacts**

Honor `nominal_only`, `selected`, and `full`; require every hard-spec golden
profile at all 45 configured points. Write `profile_summary.json`,
`stability.confirmed.json`, and `closed_loop_slew.confirmed.json` only after
fresh replay/PVT pass. Bind candidate, configuration, profile summary,
testbench, netlist, and measurement hashes into publication and result manifests.

- [ ] **Step 4: Run focused tests**

Run: `& $py -m pytest smic180/tests/analog_opt/test_pvt.py smic180/tests/analog_opt/test_report.py smic180/tests/analog_opt/test_workflow.py -q`

Expected: PASS; incomplete or stale profile evidence cannot publish.

- [ ] **Step 5: Commit**

```powershell
git add smic180/skills/smic180-simulator/analog_opt/pvt.py smic180/skills/smic180-simulator/analog_opt/workflow.py smic180/skills/smic180-simulator/analog_opt/report.py
git add smic180/tests/analog_opt/test_pvt.py smic180/tests/analog_opt/test_report.py smic180/tests/analog_opt/test_workflow.py
git commit -m 'feat: gate publication on verification profiles'
```

### Task 9: Build Independent Final Testbenches for Every Profile

**Files:**
- Modify: `smic180/skills/smic180-simulator/analog_opt/final_validation.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/final_validation_live.py`
- Test: `smic180/tests/analog_opt/test_final_validation.py`

- [ ] **Step 1: Write failing isolation and replay tests**

```python
def test_final_profiles_are_persistent_and_isolated(context):
    plan = build_final_profile_plan(context)
    assert len({p.final_testbench_cell for p in plan}) == len(plan)
    assert all(p.final_testbench_cell not in context.source_cells for p in plan)

def test_confirmation_requires_all_profile_replays(tmp_path):
    with pytest.raises(FinalValidationError, match='closed_loop_slew'):
        write_profile_confirmation(tmp_path, PASSED_WITHOUT_SLEW)
```

- [ ] **Step 2: Run and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_final_validation.py -q`

Expected: FAIL because final validation accepts only one testbench.

- [ ] **Step 3: Implement persistent per-profile final replay**

Create distinct non-overwriting final cells from prepared profile sources,
retarget each DUT to the published result cell, run `schCheck`, export fresh
netlists, confirm profile topology, and replay nominal plus configured PVT.
Confirmation contains every profile ID and its signature/hash chain; failed
cells and evidence remain for diagnosis.

- [ ] **Step 4: Run focused tests**

Run: `& $py -m pytest smic180/tests/analog_opt/test_final_validation.py smic180/tests/analog_opt/test_cli.py -q`

Expected: PASS; legacy CLI options still map to the default profile.

- [ ] **Step 5: Commit**

```powershell
git add smic180/skills/smic180-simulator/analog_opt/final_validation.py smic180/skills/smic180-simulator/analog_opt/final_validation_live.py
git add smic180/tests/analog_opt/test_final_validation.py
git commit -m 'feat: validate result cell with every profile'
```

### Task 10: Cross-Check AC, STB, and Transient in Maestro

**Files:**
- Modify: `smic180/skills/smic180-simulator/analog_opt/maestro_validation.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/maestro_validation_live.py`
- Test: `smic180/tests/analog_opt/test_maestro_validation.py`

- [ ] **Step 1: Write failing multi-analysis confirmation tests**

```python
def test_maestro_confirmation_requires_three_histories(tmp_path):
    checks = {'open_loop': True, 'stability': True, 'closed_loop_slew': False}
    with pytest.raises(MaestroValidationError, match='closed_loop_slew'):
        write_maestro_profile_confirmation(tmp_path, checks, DETAILS)

def test_maestro_metrics_match_direct_spectre():
    compare_profile_metrics(DIRECT, MAESTRO, tolerances={'relative': 1e-3, 'absolute': 1e-9})
```

- [ ] **Step 2: Run and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_maestro_validation.py -q`

Expected: FAIL because Maestro context represents one test/history.

- [ ] **Step 3: Implement profile-aware Maestro validation**

Create or update independent Maestro tests for open-loop AC, STB, and closed-loop
transient without renaming existing cells. Verify selected result histories,
reopen persistence, corner count, netlist/result hashes, and metric agreement
within explicit tolerances. STB agreement compares loop gain and margins; slew
agreement compares positive and negative fitted slopes.

- [ ] **Step 4: Run focused tests**

Run: `& $py -m pytest smic180/tests/analog_opt/test_maestro_validation.py smic180/tests/analog_opt/test_cli.py -q`

Expected: PASS and old single-test Maestro commands remain valid.

- [ ] **Step 5: Commit**

```powershell
git add smic180/skills/smic180-simulator/analog_opt/maestro_validation.py smic180/skills/smic180-simulator/analog_opt/maestro_validation_live.py
git add smic180/tests/analog_opt/test_maestro_validation.py
git commit -m 'feat: cross-check analog profiles in Maestro'
```

### Task 11: Extend Designer Handoff and Truthful Reporting

**Files:**
- Modify: `smic180/skills/smic180-analog-designer/analog_design/adapters/optimizer_v2.py`
- Modify: `smic180/skills/smic180-analog-designer/analog_design/workflow.py`
- Modify: `smic180/skills/smic180-analog-designer/analog_design/report.py`
- Modify: `smic180/skills/smic180-analog-designer/analog_design/cli.py`
- Test: `smic180/tests/analog_design/test_optimizer_v2_adapter.py`
- Test: `smic180/tests/analog_design/test_adapter_workflow_gates.py`
- Test: `smic180/tests/analog_design/test_design_report.py`
- Test: `smic180/tests/analog_design/test_designer_cli_handoffs.py`

- [ ] **Step 1: Write failing handoff and reporting tests**

```python
def test_handoff_emits_three_profiles(golden_ir, evidence):
    outputs = prepare_optimizer_v2_handoff(golden_ir, evidence=evidence)
    assert [p['id'] for p in outputs.config['verification_profiles']] == [
        'open_loop', 'stability', 'closed_loop_slew'
    ]
```

Extend `test_complete_report_summarizes_verified_results_and_keeps_unsupported_metrics_unverified`
with profile confirmation files and assert that matching hash-bound confirmations
promote only their own metrics; remove or alter either hash and assert both
verification-scope entries remain `unverified`.

- [ ] **Step 2: Run and verify red state**

Run: `& $py -m pytest smic180/tests/analog_design/test_optimizer_v2_adapter.py smic180/tests/analog_design/test_adapter_workflow_gates.py smic180/tests/analog_design/test_design_report.py -q`

Expected: FAIL because Designer emits and binds only the legacy testbench.

- [ ] **Step 3: Add profile intent and confirmation binding**

Map IR analysis intent to profiles without duplicating Optimizer V2 evaluation.
Accept profile cell/evidence inputs in the CLI, bind profile-summary,
stability, slew, final-validation, and Maestro confirmations by hash, and advance
Designer workflow only when all configured hard-profile gates pass. Historical
runs and reports remain immutable and retain their unverified labels.

- [ ] **Step 4: Run focused tests**

Run: `& $py -m pytest smic180/tests/analog_design/test_optimizer_v2_adapter.py smic180/tests/analog_design/test_adapter_workflow_gates.py smic180/tests/analog_design/test_design_report.py smic180/tests/analog_design/test_designer_cli_handoffs.py -q`

Expected: PASS; missing confirmations cannot promote phase margin or slew.

- [ ] **Step 5: Commit**

```powershell
git add smic180/skills/smic180-analog-designer/analog_design/adapters/optimizer_v2.py smic180/skills/smic180-analog-designer/analog_design/workflow.py
git add smic180/skills/smic180-analog-designer/analog_design/report.py smic180/skills/smic180-analog-designer/analog_design/cli.py smic180/tests/analog_design
git commit -m 'feat: bind analog profile verification evidence'
```

### Task 12: Discover and Prepare Real SMIC180 Profile Testbenches

**Files:**
- Create: `smic180/skills/smic180-simulator/scripts/analog_profile_prepare.py`
- Modify: `smic180/skills/smic180-simulator/scripts/analog_optimize.py`
- Modify: `smic180/skills/smic180-simulator/analog_opt/profile_testbenches.py`
- Test: `smic180/tests/analog_opt/test_profile_testbenches.py`
- Test: `smic180/tests/analog_opt/test_cli.py`

- [ ] **Step 1: Write failing plan-only and overwrite-protection tests**

```python
def test_prepare_profiles_plan_only_writes_no_cells(fake_client, request):
    plan = prepare_profiles(fake_client, request, plan_only=True)
    assert fake_client.created_cells == []
    assert [p['role'] for p in plan['profiles']] == [
        'open_loop', 'unity_gain_stability', 'unity_gain_slew'
    ]

def test_prepare_profiles_refuses_existing_target(fake_client, request):
    fake_client.existing_cells.add(request['profiles'][0]['target_cell'])
    with pytest.raises(ProfileTestbenchError, match='already exists'):
        prepare_profiles(fake_client, request, plan_only=False)
```

- [ ] **Step 2: Run and verify red state**

Run: `& $py -m pytest smic180/tests/analog_opt/test_profile_testbenches.py smic180/tests/analog_opt/test_cli.py -q`

Expected: FAIL because the preparation CLI and builder entry point do not exist.

- [ ] **Step 3: Implement evidence-driven creation**

Accept a strict request generated from Designer IR and confirmed technology
profile. Query every DUT/analogLib master, view, terminal, CDF parameter, and
net before creation. Build uniquely named cells, run callbacks, `schCheck`,
save, close/reopen readback, export fresh netlists, and confirm them. The STB
cell uses queried `analogLib/iprobe` PLUS/MINUS order; the slew cell uses queried
`vsource` pulse fields and explicit load. Refuse ambiguity and all existing cells.

- [ ] **Step 4: Run offline tests, then the disposable live experiment**

```powershell
& $py -m pytest smic180/tests/analog_opt/test_profile_testbenches.py smic180/tests/analog_opt/test_cli.py -q
$prep = 'smic180/skills/smic180-simulator/scripts/analog_profile_prepare.py'
$request = 'smic180/_local/analog_design/profile_testbench_request.json'
& $py $prep --request $request --plan-only
& $py $prep --request $request --execute
```

Expected: tests PASS; live output records actual cells, connectivity, callbacks,
`schCheck`, netlist hashes, Spectre analysis names, PSF result/trace names, and
one successful nominal STB plus one successful closed-loop pulse run. Keep the
live output under `AMS_OUTPUT_ROOT`; do not commit `_local`, netlists, or PSF.

- [ ] **Step 5: Commit code and sanitized field names only**

```powershell
git add smic180/skills/smic180-simulator/scripts/analog_profile_prepare.py smic180/skills/smic180-simulator/scripts/analog_optimize.py
git add smic180/skills/smic180-simulator/analog_opt/profile_testbenches.py smic180/tests/analog_opt/test_profile_testbenches.py smic180/tests/analog_opt/test_cli.py
git commit -m 'feat: prepare verified SMIC180 profile testbenches'
```

### Task 13: Run the Real Golden Example, Audit, and Publish the Branch

**Files:**
- Create: `smic180/docs/superpowers/checkpoints/2026-07-14-analog-multibench-stb-slew.md`
- Modify: `smic180/docs/superpowers/specs/2026-07-14-analog-design-completion-audit.md`

- [ ] **Step 1: Create a fresh formal run from live preparation evidence**

```powershell
$script = 'smic180/skills/smic180-simulator/scripts/analog_optimize.py'
$bundle = Get-Content -Raw (Join-Path $env:AMS_OUTPUT_ROOT 'analog_design/profile_prepare.latest.json') | ConvertFrom-Json
$config = $bundle.optimizer_config
$candidate = $bundle.baseline_candidate
& $py $script validate --config $config
& $py $script evaluate --config $config --candidate $candidate
```

Expected: validation and baseline exit `0`; all three profiles produce fresh,
finite measurements, plausible OP rails, profile netlist hashes, and passing or
explicitly near-feasible hard specs. Phase margin comes only from STB and slew
comes only from the closed-loop transient.

- [ ] **Step 2: Run a non-publishing trial and inspect failures**

```powershell
& $py $script run --config $bundle.trial_config
& $py $script report --run-dir $bundle.trial_run_dir
```

Expected: the three-evaluation trial completes through fresh replay/PVT only if
its hard specs pass; otherwise it stops without publication and records the
profile ID, stage, logs, OP data, netlist, and measurement evidence. Resolve any
environment or testbench defect before the formal run; do not widen specs to
hide a defect.

- [ ] **Step 3: Run formal optimization and all publication gates**

```powershell
& $py $script run --config $config
& $py $script verify-result --run-dir $bundle.formal_run_dir
& $py $script create-maestro --run-dir $bundle.formal_run_dir
& $py $script verify-maestro --run-dir $bundle.formal_run_dir --timeout 1800
```

Expected: fresh best replay passes all hard specs; all three profiles pass the
45-point PVT matrix; publication is hash-bound; independent final profile cells
pass nominal/PVT; Maestro AC/STB/transient histories agree within tolerance.

- [ ] **Step 4: Bind Designer evidence and run complete regression**

```powershell
$designer = 'smic180/skills/smic180-analog-designer/scripts/analog_design.py'
& $py $designer bind-optimizer-run --run-dir $bundle.designer_run_dir --optimizer-run-dir $bundle.formal_run_dir --expected-pvt-points 45
& $py -m pytest smic180/tests/analog_design smic180/tests/analog_opt smic180/tests/sim_io -ra
& $py -m compileall -q smic180/skills/smic180-analog-designer smic180/skills/smic180-simulator
git diff --check
```

Expected: Designer reaches final validation only from matching confirmations;
all tests pass with only the documented Windows symlink skip; compile and diff
checks exit `0`. The checkpoint records commands, timestamps, versions, cells,
metric/PVT summaries, hashes, failures, residual risks, and the distinction
between new evidence and the immutable 2026-07-13 historical result.

- [ ] **Step 5: Commit the audited evidence summary and push**

```powershell
git add smic180/docs/superpowers/checkpoints/2026-07-14-analog-multibench-stb-slew.md
git add smic180/docs/superpowers/specs/2026-07-14-analog-design-completion-audit.md
git commit -m 'docs: record multi-bench analog validation'
git push msmtx codex/smic180-analog-integration
```

Expected: runtime data, `_local/site.yaml`, licenses, local paths, raw PSF, and
temporary cells are absent from the commit; Draft PR 131 contains the reviewed
implementation and sanitized audit only.

## Completion Gate

Do not mark this plan complete unless the same published candidate passes true
STB phase/gain margin, standard positive/negative closed-loop slew, fresh replay,
45-point PVT, independent final profile testbenches, Maestro cross-check, Designer
confirmation binding, and the full offline regression. Monte Carlo and
layout/DRC/LVS/PEX remain explicitly separate follow-on plans.

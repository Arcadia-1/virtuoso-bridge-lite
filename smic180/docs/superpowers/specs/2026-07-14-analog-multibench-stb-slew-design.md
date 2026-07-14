# SMIC180 Multi-Bench STB and Closed-Loop Slew Design

**Date:** 2026-07-14

**Branch:** `codex/smic180-analog-integration`

**Scope:** Add true STB loop-stability and standard closed-loop slew
verification. Monte Carlo and layout/DRC/LVS/PEX are separate follow-on
specifications that consume the multi-bench boundary defined here.

## 1. Motivation

The published two-stage Miller amplifier already has verified operating-point,
open-loop AC, PVT, publication, independent final-testbench, and Maestro
evidence. Phase margin and closed-loop slew remain correctly unverified:

- ordinary AC is not a loop-gain analysis;
- the existing transient is an open-loop diagnostic, not a unity-gain follower
  large-signal test.

These measurements require different feedback and stimulus topology. They must
not be derived from the existing AC or transient data.

## 2. Verified Environment Facts

The following facts were queried from the live installation:

- Virtuoso is IC6.1.8-64b.
- Spectre is 18.1.0.077 64-bit.
- Spectre supports `stb` with an explicit `probe` instance.
- `analogLib/iprobe/symbol` exists.
- Its terminals are `PLUS` and `MINUS`, both bidirectional.
- Its Spectre CDF term order is `(PLUS MINUS)` and component name is `iprobe`.
- The installed `vsource` supports pulse `val0`, `val1`, `period`, `rise`,
  `fall`, and `width` parameters.

Production testbench cells, result names, and PSF layout still require a
disposable live round-trip experiment before becoming defaults.

## 3. Architectural Decision

Add optional **verification profiles** to Optimizer V2. A profile is an
independently netlisted and simulated testbench that references the same DUT
candidate but owns its topology, stimuli, analyses, metrics, specifications,
PVT policy, and evidence directory.

The current single-testbench configuration remains valid and is normalized to
one profile named `default`. Existing configurations, signed runs, and
publication markers remain immutable.

The golden example uses:

| Profile | Role | Analyses |
|---|---|---|
| `open_loop` | Existing small-signal verification | DC OP, AC |
| `stability` | Unity-gain feedback with `iprobe` | DC OP, STB |
| `closed_loop_slew` | Unity-gain large-signal follower | DC OP, transient |

The same boundary later supports PSRR, CMRR, noise, startup, load transient,
Monte Carlo, and post-layout verification without op-amp-specific evaluator
branches.

## 4. Configuration Model

Optimizer V2 keeps `version: 2` and gains an optional
`verification_profiles` array. Each profile contains:

- stable `id` and engineering `role`;
- `testbench_cell` and `dut_instance`;
- profile-visible stimuli;
- analyses, metrics, and hard/soft specifications;
- PVT policy: `nominal_only`, `selected`, or `full`;
- freshness requirements and optional timeout.

Physical parameters remain top-level and shared. A candidate is applied to the
work DUT once, then every required profile is freshly exported and simulated.

```yaml
version: 2
verification_profiles:
  - id: stability
    role: unity_gain_stability
    testbench_cell: miller_stability_tb
    dut_instance: DUT
    analyses:
      - name: loop
        type: stb
        probe: IPRB
        start: 1.0
        stop: 1000000000.0
        points_per_decade: 50
    pvt_policy: full
```

Names above illustrate structure only. Production names come from generated
testbench evidence.

## 5. Execution Model

For every baseline or candidate:

1. Apply physical CDF parameters to the work DUT as one transaction.
2. Run callbacks, `schCheck`, save, close, reopen, and read back values.
3. Confirm matching and width/finger invariants.
4. For each required profile:
   - copy a prepared testbench to a disposable cell;
   - replace only its DUT master with the work DUT;
   - export a fresh netlist;
   - patch only declared stimuli and conditions;
   - confirm DUT, probe, sources, analyses, and physical values;
   - run Spectre in a new profile directory;
   - parse fresh results and evaluate profile specifications.
5. Merge profile-qualified metrics.
6. Return a finite penalty and structured failure if any required profile fails.

Metric namespaces include:

```text
stb.stability.loop.phase_margin_deg
stb.stability.loop.gain_margin_db
stb.stability.loop.unity_loop_frequency_hz
tran.closed_loop_slew.step.VOUT.slew_rise_v_per_s
tran.closed_loop_slew.step.VOUT.slew_fall_v_per_s
```

Open-loop diagnostics and closed-loop acceptance metrics never share names.

## 6. STB Profile

The DUT is a unity-gain follower at the configured common-mode and load. A real
`analogLib/iprobe` is inserted in the feedback path with orientation recorded
in the manifest. The probe must preserve the closed-loop DC operating point.

The builder records probe master/view, instance name, `PLUS/MINUS` nets, CDF
mapping, feedback direction, local ground, load, sources, `schCheck`, and
exported-netlist hash.

The STB parser consumes fresh complex loop-gain data and reports low-frequency
loop gain, unity-loop-gain crossover, phase margin, gain margin, associated
crossings, and ambiguity diagnostics. It unwraps phase and interpolates on
logarithmic frequency; it does not reuse ordinary AC phase calculations.

The profile fails for an absent or incorrect probe, topology mismatch, stale or
non-finite data, insufficient samples, missing required crossing, ambiguous
crossing under the selected policy, implausible DC point, or failed hard spec.
An undefined margin is never converted to a passing number.

## 7. Closed-Loop Slew Profile

The DUT is a unity-gain follower. The non-inverting input receives a configured
large-signal pulse around a valid common-mode point; the inverting input is
connected to the output. Output load and pulse settings are explicit.

Required configuration includes low/high levels, delay, rise/fall time, width,
period, load, transient stop/maxstep, output node, fractional thresholds, and
settling tolerance.

Positive and negative slew are separate metrics. The default method performs a
least-squares fit over the 20 to 80 percent output transition rather than using
the largest adjacent-sample derivative. Evidence records thresholds, interval,
sample count, fit residual, and method.

The profile also reports delays, settling time, overshoot, undershoot, and
final-value error. It fails when a transition is absent, clipped, too
non-monotonic, insufficiently sampled, or does not settle in the run window.

## 8. Search, PVT, and Publication Gates

Every profile with a hard specification runs for every candidate. A profile may
be skipped during search only when all of its metrics are report-only.

Publication requires:

1. reproducible baseline for every required profile;
2. search history with profile-specific failures;
3. fresh best replay of every profile;
4. configured PVT replay for every hard-spec profile;
5. aggregate hard-spec pass;
6. result publication bound to candidate and profile-summary hashes;
7. independent persistent final testbenches for every profile;
8. fresh result-cell nominal and PVT replay;
9. Maestro AC/STB/transient cross-check for the golden example.

The golden example runs the current 45-point PVT matrix for all three profiles.
A reduced search screen may be added later, but full PVT remains mandatory
before publication.

## 9. Artifacts

```text
<run>/
  profiles/<profile_id>/
  candidates/<candidate_id>/profiles/<profile_id>/
  best_replay/profiles/<profile_id>/
  pvt/<point_id>/profiles/<profile_id>/
  final_validation/profiles/<profile_id>/
  profile_summary.json
  stability.confirmed.json
  closed_loop_slew.confirmed.json
```

Each manifest records configuration hash, candidate hash, testbench signature,
netlist hash, simulator version, timestamps, result location, metrics, specs,
and failure classification.

Historical reports remain unchanged. New reports promote phase margin or
closed-loop slew from `unverified` only when the corresponding confirmation and
hashes validate.

## 10. Module Boundaries

- `schema`: parse profiles and preserve legacy behavior.
- `analyses`: validate and render STB structurally.
- `profile`: profile model and aggregate validation.
- `netlist`: prepare, export, patch, and confirm each testbench.
- `metrics`: parse STB and robust closed-loop transient data.
- `workflow`: apply one candidate and orchestrate profiles.
- `pvt`: evaluate each profile according to policy.
- `final_validation`: create and replay result-cell testbenches.
- `maestro_validation`: verify AC/STB/transient histories.
- Designer adapter/report: generate intent and consume confirmations without
  duplicating Optimizer V2.

## 11. Compatibility

- V2 JSON without profiles behaves exactly as before.
- Existing metric names and artifacts remain readable.
- Existing source, work, result, testbench, and Maestro cells are not renamed or
  overwritten.
- Signed run directories are never migrated in place.
- Multi-profile runs use fresh cells and directories.
- CLI additions are backward-compatible.

## 12. Tests

Offline coverage includes legacy normalization, profile schema failures,
deterministic STB generation, probe confirmation, loop-gain phase/crossing
math, missing and multiple crossings, robust positive/negative slew fitting,
clipping and non-settling cases, aggregate penalties, profile PVT policy,
publication enforcement, final-testbench isolation, hashes, truthful reporting,
and resume between profiles.

Live coverage proves real analogLib testbench creation, connectivity readback,
`schCheck`, fresh `si` export, STB PSF discovery, agreement with Maestro,
closed-loop pulse and waveform metrics, baseline, a non-publishing trial,
fresh replay, 45-point PVT, publication, independent result replay, and Maestro
cross-check.

## 13. Follow-On Work

After live completion:

1. Monte Carlo becomes a profile family with statistics discovery,
   deterministic seeds, process/mismatch modes, yield, confidence intervals,
   and publication thresholds.
2. Layout adds physical intent, matching/common-centroid and guard-ring rules,
   DRC/LVS/PEX gates, extracted-view selection, and the same profiles on the
   post-layout netlist.

Neither follow-on may claim completion from schematic-only or nominal-only
evidence.

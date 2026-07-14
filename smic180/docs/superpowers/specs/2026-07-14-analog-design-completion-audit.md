# SMIC180 Analog Design Workflow Completion Audit

**Date:** 2026-07-14
**Branch:** `codex/smic180-analog-integration`
**Live design run:** `live_cli_20260713_153857`
**Live optimizer run:** `formal_v2_publish_20260713_203000/run`

## Audit Method

This audit separates three evidence classes:

1. **Code gate:** production logic rejects incomplete, stale, unsafe, or mismatched evidence.
2. **Regression evidence:** an isolated automated test proves the gate or transformation.
3. **Live evidence:** a fresh SMIC180 Virtuoso/Spectre artifact proves the real PDK boundary.

A test pass alone is not treated as live-PDK proof. Historical confirmation files were not edited. New schema, calculation, and expanded report artifacts were generated under the additive `audit/addendum-v1/` snapshot after the complete historical confirmation chain was reverified.

## Acceptance Matrix

| Requirement | Authoritative evidence | Status |
|---|---|---|
| Versioned design specification with strict SI handling and metric classes | `inputs/design_spec.json`; schema tests; unit/spec regression tests | Proven |
| Registered, explainable topology selection without arbitrary topology generation | `topology/topology_plan.json`; Miller topology tests | Proven |
| Initial sizing records formula, inputs, assumptions, units, status, and confidence | `sizing/initial_sizing.json`; additive `sizing/calculation_report.md` | Proven |
| Versioned Circuit IR is authoritative before handoff | `ir/circuit_ir.json`; canonical digest and IR validation tests | Proven |
| Same IR deterministically generates the direct Spectre deck | deterministic writer tests; `windows_sim/generated/design.scs` | Proven |
| Fresh nominal Spectre run produces readable measurements and OP diagnostics | `windows_sim/iterations/0001/`; `windows_nominal_passed.confirmed.json` | Proven |
| Candidate freeze is immutable and hash bound | `frozen/candidate_manifest.json`; `candidate_frozen.confirmed.json` | Proven |
| Real SMIC180 schematic is created without overwriting the source | `virtuoso/live_target.json`; materialization confirmation; overwrite refusal tests | Proven |
| Real PDK masters, terminals, and CDF mappings are evidence backed | confirmed `ir/technology_profile.json`; live discovery evidence | Proven |
| CDF callback, close/reopen readback, and physical normalization pass | `virtuoso/cdf_readback.json`; `cdf_roundtrip.confirmed.json` | Proven |
| `schCheck` and save pass | `virtuoso/schcheck.json`; `schematic_checked.confirmed.json` | Proven |
| Virtuoso `si` netlist exports and runs independently | `virtuoso/exported_netlist.scs`; `equivalence/virtuoso_iterations/0001/` | Proven |
| Direct and Virtuoso netlists are structurally equivalent | `equivalence/structural_comparison.json` reports no differences | Proven |
| Fresh simulation results are equivalent within explicit tolerances | `equivalence/simulation_comparison.json`; equivalence confirmation | Proven |
| Simulator handoff follows reviewed analog pin and testbench intent | `simulator/pin_classifications.json`, `sim_config.json`, `review_required.json` | Proven |
| Existing simulator executes fresh and produces readable results | `simulator/external_validation.json`; `simulator_validated.confirmed.json` | Proven |
| Optimizer V2 handoff uses real instances/CDF properties and linked matching variables | `optimizer/analog_opt_v2.json`, baseline and CDF mapping evidence | Proven |
| Baseline evaluation and optimization use distinct source/work/result cells | optimizer run manifest and cell names; safety regression tests | Proven |
| Best candidate is freshly replayed before acceptance | optimizer workflow and result manifest; designer optimizer binding gate | Proven |
| Full configured PVT matrix passes | 45/45 points, five process corners, three voltages, three temperatures | Proven |
| Publication is candidate-hash bound | publication hash `60a6b817e22f83a0b46f0cf4b644f4e1719369c8c0b9fd91227c9fd22a34077e` | Proven |
| Published result is retested with an independent persistent testbench | `final_validation/final_validation.confirmed.json` | Proven |
| Maestro setup survives reopen and all configured corners pass | `maestro_validation.confirmed.json`, history `Interactive.3`, 45 corners, zero failed | Proven |
| Workflow is resumable and detects evidence tampering | confirmation-hash resume tests and live chain revalidation | Proven |
| Each new stage records status, time, input summary, and output summary | `manifests/*.json`; root manifest update tests | Proven for new runs |
| Historical live run gains new artifacts without changing signed history | `audit/addendum-v1/migration_manifest.json`; before/after hashes equal | Proven |
| `.latest_run` uses atomic replacement in the standard output layout | `ArtifactStore.write_text()` plus workflow initialization regression | Proven |
| Report includes final parameters, OP, optimization history, PVT ranges, final cells, and risks | additive `reports/design_report.json` and `.md`; report regression | Proven |
| Unverified metrics are never reported as passed | phase margin and standard closed-loop slew rate remain explicitly unverified | Proven |
| Designer, Optimizer V2, and Simulator remain regression compatible | fresh combined regression: 727 passed, 1 existing skip | Proven |

## Live Result Summary

- Published cell: `amp_text/codex_miller_final_r_20260713_203000`
- Independent final testbench: `codex_miller_final_r_20260713_203000_tb`
- Maestro setup: `codex_miller_final_r_20260713_203000_maestro`
- Final parameters are recorded in the additive design report.
- Nominal gain: `64.923913 dB`
- Nominal unity-gain bandwidth: `22.029488 MHz`
- PVT minimum gain: `60.720195 dB`
- PVT minimum unity-gain bandwidth: `10.078441 MHz`
- PVT result: `45/45` passed

## Residual Scope

- Phase margin is not verified because the accepted flow has no dedicated STB loop testbench. Ordinary AC is not used as a substitute.
- Standard closed-loop slew rate is not verified because the existing transient slope is an open-loop diagnostic, not a follower large-signal slew test.
- Layout, DRC/LVS/PEX, Monte Carlo, mismatch yield, and post-layout optimization remain outside version-1 scope.

## Final Gate

The final gate passed: the fresh combined regression reported 727 passed and 1 existing skip; both CLIs loaded; Python compilation and `git diff --check` passed; no runtime data, local configuration, license data, or machine-specific paths are included in the implementation changes.

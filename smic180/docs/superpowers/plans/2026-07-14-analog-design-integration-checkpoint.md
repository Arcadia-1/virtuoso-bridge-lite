# SMIC180 Analog Design Integration Checkpoint

## Resume location

- Worktree: `D:\Codex_project\virtuoso_bridge\.worktrees\smic180-analog-integration`
- Branch: `codex/smic180-analog-integration`
- Checkpoint commit: inspect `HEAD` after checkout.
- Full goal remains active; do not mark complete yet.

## Completed in this checkpoint

- Merged the Designer and Optimizer V2 feature histories in an isolated integration worktree.
- Added the complete simulator public package required by Designer and Optimizer tests.
- Added Designer CLI commands: `materialize`, `verify-equivalence`, `prepare-simulator`, and `prepare-optimizer`.
- Added versioned `design_spec.schema.json` and `circuit_ir.schema.json` generation.
- Added `sizing/calculation_report.md` generation with formulas, inputs, assumptions, units, status, and confidence.
- Added a default live materialization bridge using the established headless Virtuoso client and simulator `si` exporter.
- Full offline regression passed before this checkpoint: Designer, Optimizer V2, and Simulator suites; one existing test was skipped.

## Next work

1. Re-run the complete regression and `git diff --check` from the clean checkpoint.
2. Audit every requirement in the goal objective against code and the existing live run evidence.
3. Regenerate the existing live run report/schema/calculation artifacts without invalidating signed historical evidence, or create an explicit migration artifact.
4. Verify CLI live boundaries with plan-only/offline fixtures and inspect any missing resume/manifest requirements.
5. Decide integration into `main` only after the completion audit; the main checkout has extensive unrelated user changes and must not be modified or cleaned automatically.

## Safety

- Do not delete existing Virtuoso cells or runtime evidence.
- Do not overwrite `main` working-tree changes.
- Do not commit `_local/site.yaml`, licenses, local paths, or simulation raw output.
- Ordinary AC does not prove phase margin; open-loop transient does not prove standard closed-loop slew rate.
## Completion audit update

- Fresh baseline before the audit: 721 passed, 1 skipped.
- Added per-stage manifests, timestamps, input/output summaries, failure manifests,
  root-manifest status updates, and standard-layout `.latest_run` updates.
- Added `audit-run`, which created `audit/addendum-v1/` for the formal live run
  without changing historical signed control hashes.
- Expanded the report with final parameters, per-device operating-point data,
  optimization history, and numeric PVT ranges.
- Fixed isolated Designer regression setup so `smic180/tests/analog_design` can
  run without depending on Optimizer test collection order.
- Completion matrix: `../specs/2026-07-14-analog-design-completion-audit.md`.
- Final combined regression passed: 727 passed, 1 existing skip. CLI smoke, compilation, `git diff --check`, and change-scope hygiene checks passed.

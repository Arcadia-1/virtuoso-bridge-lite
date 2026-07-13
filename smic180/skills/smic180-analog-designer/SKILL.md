---
name: smic180-analog-designer
description: Design SMIC180 analog circuits from structured specifications through initial sizing, Circuit IR, deterministic Spectre candidate simulation, Virtuoso schematic handoff, round-trip verification, and preparation for the existing SMIC180 simulator and Optimizer V2 workflows. Use for SMIC180 op-amp, OTA, comparator, LDO, bandgap, or other analog circuit design tasks; do not use for IO-ring generation or simulation-only requests.
---

# SMIC180 Analog Designer

Build analog designs through an auditable Windows-orchestrated flow. Keep
`circuit_ir.json` authoritative before Virtuoso handoff. Treat reopened CDF
values, `schCheck`, exported netlists, and fresh Spectre results as authoritative
after handoff.

## Guardrails

- Keep this skill independent from `smic180-simulator` and
  `smic180-analog-optimizer-v2`; invoke their public boundaries for downstream
  validation and optimization.
- Do not guess PDK master names, terminals, CDF properties, units, callbacks, or
  legal dimensions.
- Do not overwrite existing Virtuoso cells without explicit authorization.
- Do not claim phase margin from ordinary AC analysis.
- Do not write confirmation artifacts from stale, missing, or partial evidence.

## Current workflow

1. Validate a version-1 design specification.
2. Select a registered topology and calculate an initial engineering seed.
3. Build and validate version-1 Circuit IR.
4. Generate a deterministic Spectre deck and run fresh nominal analyses.
5. Freeze an immutable candidate only after its configured gate passes.
6. Materialize it with a confirmed SMIC180 technology profile.
7. Reopen CDF values, run `schCheck`, export with `si`, and prove equivalence.
8. Prepare reviewed simulator and Optimizer V2 handoffs.

Read the matching reference before executing a live PDK or topology-specific
stage. Runtime artifacts belong under `${AMS_OUTPUT_ROOT}/analog_design/`.

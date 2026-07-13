# SMIC180 Analog Design Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independent `smic180-analog-designer` skill that turns a versioned analog design specification into a deterministic Circuit IR and Spectre design loop, materializes a frozen candidate in real SMIC180 Virtuoso, proves round-trip equivalence, and hands the verified design to the existing simulator and Optimizer V2 workflows.

**Architecture:** A versioned Circuit IR is authoritative before Virtuoso handoff. Focused topology, sizing, technology, netlist, simulation, Virtuoso, adapter, workflow, and report modules communicate through JSON artifacts with hashes and narrow confirmation records. After handoff, reopened CDF data, `schCheck`, Virtuoso-exported netlists, fresh Spectre results, and existing Optimizer V2 state are authoritative.

**Tech Stack:** Python 3 dataclasses and standard library, JSON Schema-style validation implemented locally, pytest, Spectre, Cadence Virtuoso/SKILL through `virtuoso-bridge-lite`, existing SMIC180 site configuration, simulator, and Optimizer V2 CLIs.

---

## File Map

Create production code only under `smic180/skills/smic180-analog-designer/`; create tests under `smic180/tests/analog_design/`. Modify `smic180/README.md` and `smic180/AGENTS.md` only after the independent skill is working. Do not modify simulator or optimizer core unless an integration test proves a public-interface gap.

## Milestone 1: Offline Design Core

### Task 1: Skill Skeleton, Strict JSON, Units, and Specification

**Files:**
- Create: `smic180/skills/smic180-analog-designer/SKILL.md`
- Create: `smic180/skills/smic180-analog-designer/agents/openai.yaml`
- Create: `smic180/skills/smic180-analog-designer/analog_design/__init__.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/jsonio.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/units.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/spec.py`
- Create: `smic180/tests/analog_design/conftest.py`
- Create: `smic180/tests/analog_design/test_units.py`
- Create: `smic180/tests/analog_design/test_spec.py`

- [ ] Write tests proving non-finite JSON is rejected, supported SI quantities normalize to floats, booleans are not numbers, metric kinds are restricted to `hard/objective/report`, and AC cannot claim phase margin.
- [ ] Run `python -m pytest smic180/tests/analog_design/test_units.py smic180/tests/analog_design/test_spec.py -v`; expect import failures.
- [ ] Implement `parse_quantity(value, dimension)`, strict JSON loading, frozen `MetricSpec` and `DesignSpec` dataclasses, and `load_design_spec(path)` with exact field validation.
- [ ] Rerun the focused tests; expect PASS.
- [ ] Validate the skill metadata with the skill-creator validator and commit `feat: add SMIC180 analog design specification core`.

### Task 2: Circuit IR and Electrical Validation

**Files:**
- Create: `smic180/skills/smic180-analog-designer/analog_design/ir.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/validation.py`
- Create: `smic180/skills/smic180-analog-designer/references/circuit-ir-v1.md`
- Create: `smic180/tests/analog_design/test_ir.py`
- Create: `smic180/tests/analog_design/test_validation.py`

- [ ] Write tests for required top-level fields, stable IDs, duplicate IDs, unknown nets, missing terminals, critical floating ports, matching-group consistency, invalid bounds, and unparseable units.
- [ ] Run the two test files; expect failures for missing modules.
- [ ] Implement immutable IR records, strict loader/writer, canonical JSON hashing, reference validation, connectivity checks, and matching checks.
- [ ] Rerun focused tests; expect PASS.
- [ ] Commit `feat: add versioned analog circuit IR`.

### Task 3: Technology Profile Contract

**Files:**
- Create: `smic180/skills/smic180-analog-designer/analog_design/technology/__init__.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/technology/base.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/technology/smic180.py`
- Create: `smic180/skills/smic180-analog-designer/references/smic180-live-discovery.md`
- Create: `smic180/tests/analog_design/test_technology.py`

- [ ] Write tests for stable `master_ref` resolution, terminal coverage, generic-to-CDF parameter mapping, normalization, evidence requirements, and refusal to use an unconfirmed profile for live operations.
- [ ] Run the test; expect failure.
- [ ] Implement `DeviceAdapter`, `TechnologyProfile`, confirmation evidence, fake offline profile support, and explicit unresolved-field errors. Do not include guessed real master names.
- [ ] Rerun; expect PASS.
- [ ] Commit `feat: define evidence-backed SMIC180 technology profile`.

### Task 4: Topology Registry and Two-Stage Miller Template

**Files:**
- Create: `smic180/skills/smic180-analog-designer/analog_design/topology/__init__.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/topology/base.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/topology/registry.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/topology/two_stage_miller.py`
- Create: `smic180/skills/smic180-analog-designer/references/two-stage-miller.md`
- Create: `smic180/tests/analog_design/test_topology.py`

- [ ] Write tests showing unknown topologies fail, the Miller plugin is registered, NMOS/PMOS input variants are explicit, expected roles and ports exist, matching groups are correct, and the nulling resistor slot is disabled but represented.
- [ ] Run; expect failure.
- [ ] Implement the plugin protocol, registry, explainable topology plan, and deterministic Miller structural plan independent of Cadence master names.
- [ ] Rerun; expect PASS.
- [ ] Commit `feat: add two-stage Miller topology plugin`.

### Task 5: Initial Sizing with Provenance

**Files:**
- Create: `smic180/skills/smic180-analog-designer/analog_design/sizing/__init__.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/sizing/base.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/sizing/square_law.py`
- Create: `smic180/tests/analog_design/test_sizing.py`

- [ ] Write tests for finite bias, gm, compensation, width/length seeds, formula IDs, assumptions, units, and separation of estimated versus confirmed values.
- [ ] Run; expect failure.
- [ ] Implement bounded engineering seed calculations and provenance records; reject infeasible or dimensionally invalid specifications instead of silently clipping hard requirements.
- [ ] Rerun; expect PASS.
- [ ] Commit `feat: add provenance-rich Miller initial sizing`.

### Task 6: Circuit IR Builder and Deterministic Spectre AST

**Files:**
- Create: `smic180/skills/smic180-analog-designer/analog_design/builder.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/netlist/__init__.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/netlist/ast.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/netlist/spectre_writer.py`
- Create: `smic180/tests/analog_design/test_builder.py`
- Create: `smic180/tests/analog_design/test_spectre_writer.py`

- [ ] Write tests proving specification + topology + sizing create valid IR, equal IR emits byte-identical decks, numeric formatting is stable, ordering is canonical, includes come from the technology/site contract, and IR hash is embedded.
- [ ] Run; expect failure.
- [ ] Implement the IR builder and typed Spectre AST/writer for parameters, includes, subcircuits, device instances, DC-op, AC, transient, noise, save, and options.
- [ ] Rerun; expect PASS.
- [ ] Commit `feat: generate deterministic Spectre designs from circuit IR`.

### Task 7: Offline Workflow, Artifacts, CLI, and Reports

**Files:**
- Create: `smic180/skills/smic180-analog-designer/analog_design/artifacts.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/workflow.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/report.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/cli.py`
- Create: `smic180/skills/smic180-analog-designer/scripts/analog_design.py`
- Create: `smic180/tests/analog_design/test_artifacts.py`
- Create: `smic180/tests/analog_design/test_workflow.py`
- Create: `smic180/tests/analog_design/test_cli.py`

- [ ] Write tests for atomic run creation, exact state transitions, failed-attempt records, hash-checked resume, no false confirmation markers, `.latest_run`, `validate-spec`, `plan`, `build-ir`, `render-netlist`, `resume`, and truthful incomplete reports.
- [ ] Run; expect failure.
- [ ] Implement artifact writers, monotonic state machine, offline commands, and JSON/Markdown report generation.
- [ ] Rerun focused and all Milestone 1 tests; expect PASS.
- [ ] Commit `feat: add resumable offline analog design workflow`.

## Milestone 2: Direct Spectre Design Loop

### Task 8: Site Configuration and Direct Spectre Backend

**Files:**
- Create: `smic180/skills/smic180-analog-designer/analog_design/site.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/simulation/__init__.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/simulation/direct_spectre.py`
- Create: `smic180/skills/smic180-analog-designer/analog_design/simulation/diagnostics.py`
- Create: `smic180/tests/analog_design/test_site.py`
- Create: `smic180/tests/analog_design/test_direct_spectre.py`
- Create: `smic180/tests/analog_design/test_diagnostics.py`

- [ ] Write injected-runner tests for site config reuse, fresh raw directories, exit-code plus result validation, finite measurements, MOS OP diagnostics, stale/missing data rejection, and categorized failures.
- [ ] Run; expect failure.
- [ ] Implement the backend using public bridge-lite/Spectre boundaries and existing PSF readers where stable; keep live construction injected.
- [ ] Rerun; expect PASS.
- [ ] Commit `feat: add direct Spectre analog design loop`.

### Task 9: Iteration and Candidate Freeze

**Files:**
- Modify: `analog_design/workflow.py`
- Modify: `analog_design/cli.py`
- Create: `smic180/tests/analog_design/test_iteration.py`
- Create: `smic180/tests/analog_design/test_freeze.py`

- [ ] Write tests for immutable iteration directories, allowed parameter-only revisions, hard-pass freeze, explicit near-feasible freeze, rejected NaN/missing metrics, and digest-protected frozen IR/deck pairs.
- [ ] Run; expect failure.
- [ ] Implement `simulate` and `freeze` workflow commands and confirmation records.
- [ ] Rerun Milestones 1-2 regression; expect PASS.
- [ ] Commit `feat: freeze verified Spectre design candidates`.

## Milestone 3: Virtuoso and PDK Handoff

### Task 10: Live Technology Discovery

**Files:**
- Create: `analog_design/technology/discovery.py`
- Modify: `analog_design/cli.py`
- Create: `smic180/tests/analog_design/test_discovery.py`

- [ ] Write fake-client tests for PDK-root conflict reporting, library/master enumeration, terminal/CDF capture, disposable round-trip evidence, and confirmed-profile refusal when evidence is incomplete.
- [ ] Run; expect failure.
- [ ] Implement plan-only discovery plus injected live probes through documented bridge-lite APIs/SKILL references.
- [ ] Rerun; expect PASS.
- [ ] Run the real discovery against the VM, preserving raw evidence outside git, and review every resolved mapping before confirmation.
- [ ] Commit `feat: discover live SMIC180 device mappings` without committing local evidence.

### Task 11: Virtuoso Plan, Materialization, and Readback

**Files:**
- Create: `analog_design/virtuoso/__init__.py`
- Create: `analog_design/virtuoso/plan.py`
- Create: `analog_design/virtuoso/materialize.py`
- Create: `analog_design/virtuoso/readback.py`
- Create: `analog_design/virtuoso/export.py`
- Create: `smic180/tests/analog_design/test_virtuoso_plan.py`
- Create: `smic180/tests/analog_design/test_materialize.py`

- [ ] Write injected-client tests for destination protection, source immutability, plan-only/dry-run, master/terminal preflight, callback application, close/reopen readback, `schCheck`, save, and fresh `si` export.
- [ ] Run; expect failure.
- [ ] Implement only documented bridge-lite operations and verified SKILL probes; persist evidence for each gate.
- [ ] Rerun; expect PASS.
- [ ] Create a disposable real schematic and verify reopen/CDF/`schCheck`/export evidence.
- [ ] Commit `feat: materialize frozen analog designs in Virtuoso`.

### Task 12: Semantic and Simulation Equivalence

**Files:**
- Create: `analog_design/netlist/spectre_reader.py`
- Create: `analog_design/netlist/equivalence.py`
- Create: `smic180/tests/analog_design/test_spectre_reader.py`
- Create: `smic180/tests/analog_design/test_equivalence.py`

- [ ] Write tests for ordering/name/default tolerance, terminal-aware connectivity, parameter normalization, structural mismatches, absolute/relative metric tolerances, stale results, and guarded confirmation creation.
- [ ] Run; expect failure.
- [ ] Implement a constrained Spectre circuit parser and normalized graph comparator plus fresh dual-simulation comparison.
- [ ] Rerun; expect PASS.
- [ ] Run equivalence on the disposable live schematic.
- [ ] Commit `feat: verify direct and Virtuoso netlist equivalence`.

## Milestone 4: Existing Workflow Handoff

### Task 13: Simulator Adapter

**Files:**
- Create: `analog_design/adapters/__init__.py`
- Create: `analog_design/adapters/simulator.py`
- Create: `smic180/tests/analog_design/test_simulator_adapter.py`

- [ ] Write tests for proposed pin classifications, supply/bias/common-mode/load intent, existing simulator loader validation, review-required marker, and refusal before equivalence passes.
- [ ] Run; expect failure.
- [ ] Implement adapter output only; do not bypass the simulator review gate.
- [ ] Rerun; expect PASS.
- [ ] Commit `feat: prepare verified designs for SMIC180 simulator`.

### Task 14: Optimizer V2 Adapter

**Files:**
- Create: `analog_design/adapters/optimizer_v2.py`
- Create: `smic180/tests/analog_design/test_optimizer_adapter.py`

- [ ] Write tests for version 2 output, actual instance/CDF evidence, distinct cells, linked matching variables, physical bounds, fixed stimuli, PVT mapping, baseline candidate, and refusal without schematic/equivalence evidence.
- [ ] Run; expect failure.
- [ ] Implement adapter generation and validate through the existing Optimizer V2 public loader/CLI.
- [ ] Rerun; expect PASS.
- [ ] Run real baseline `evaluate` and a small nonpublishing trial on the golden cell.
- [ ] Commit `feat: hand analog designs to Optimizer V2`.

## Milestone 5: Full Acceptance and Documentation

### Task 15: Full Workflow Orchestration and Final Report

**Files:**
- Modify: `analog_design/workflow.py`
- Modify: `analog_design/report.py`
- Modify: `analog_design/cli.py`
- Create: `smic180/tests/analog_design/test_full_workflow.py`
- Modify: `smic180/skills/smic180-analog-designer/SKILL.md`
- Modify: `smic180/README.md`
- Modify: `smic180/AGENTS.md`

- [ ] Write orchestration tests proving no stage advances without its evidence and optimizer/final-validation states are referenced rather than duplicated.
- [ ] Run; expect failure.
- [ ] Implement remaining command orchestration and complete truthful report sections.
- [ ] Rerun all analog-design, optimizer, and simulator offline regressions; expect PASS.
- [ ] Run the real two-stage Miller flow through nominal, handoff, equivalence, simulator, Optimizer V2 search, fresh replay, PVT, publication, independent final testbench, and required Maestro verification.
- [ ] Audit every user acceptance criterion against current artifacts and test evidence; correct any defect test-first.
- [ ] Validate the skill folder and CLI help, ensure no raw simulation/local config is staged, and commit `feat: complete SMIC180 analog design workflow`.

## Required Final Commands

```powershell
$py = "D:\Codex_project\virtuoso_bridge\.venv\Scripts\python.exe"
& $py -m pytest smic180/tests/analog_design -v
& $py -m pytest smic180/tests/analog_opt smic180/tests/sim_io -v
& $py smic180/skills/smic180-analog-designer/scripts/analog_design.py --help
& $py smic180/skills/smic180-analog-optimizer-v2/scripts/analog_optimize.py --help
```

The live acceptance run must leave an auditable `${AMS_OUTPUT_ROOT}/analog_design/<timestamp>/` tree and no committed runtime artifacts.

# SMIC180 Analog Design Workflow Design

**Date:** 2026-07-13
**Status:** Proposed for user review
**Target skill:** `smic180/skills/smic180-analog-designer/`

## 1. Purpose

Add an independent SMIC180 analog-circuit design skill ahead of the existing
`smic180-simulator` and `smic180-analog-optimizer-v2` skills. The new skill owns
specification capture, constrained topology selection, initial sizing, Circuit
IR generation, direct Spectre candidate simulation, candidate freezing,
Virtuoso schematic creation, and handoff validation.

It does not implement another simulator or optimizer. After a candidate becomes
a verified Virtuoso schematic, the existing simulator and optimizer remain the
authoritative engines for testbench validation, CDF-based optimization, fresh
replay, PVT, publication, and final independent testbench verification.

The first supported topology is a two-stage Miller-compensated operational
amplifier. The workflow core must accept future topology plugins for common
source amplifiers, comparators, OTAs, LDOs, and bandgap references.

## 2. Constraints and Sources of Truth

The workflow has two authority phases:

1. Before Virtuoso handoff, `circuit_ir.json` is the single design source.
   Direct Spectre decks are deterministic generated artifacts and must never be
   edited as a persistent design source.
2. After Virtuoso handoff, the schematic database, reopened CDF readback,
   `schCheck`, the exported Virtuoso netlist, and fresh Spectre results are
   authoritative for physical implementation and signoff.

The skill uses the repository `_local/site.yaml` chain. It does not create a
second local configuration source or commit machine paths, license values, raw
simulation data, or `_local/site.yaml`.

The supplied live PDK root is `/home/IC/Tech/smic18ee_2`; current repository
configuration references `/home/IC/Tech/smic18ee_2P6M_20100810`. Live discovery
must resolve which path is authoritative before confirming a technology profile.

## 3. Existing Capabilities to Reuse

### 3.1 `virtuoso-bridge-lite`

Reuse its public APIs for structured schematic editing and reading, `schCheck`,
`dbSave`, batch `si` circuit-netlist export, remote execution and file transfer,
Spectre execution, and PSF parsing. Headless schematic creation uses
net-label-directed connectivity and never editor-window-only wire operations.

### 3.2 `smic180-simulator`

Reuse its site configuration, pin-intent rules, testbench construction, deck
configuration, raw-result loaders, metric extraction, and Maestro integration.
The designer produces reviewed handoff inputs and does not bypass pin or
testbench correctness gates.

### 3.3 `smic180-analog-optimizer-v2`

Reuse its strict schema, physical parameters, matching groups, CDF application
and readback, finite failures, search, fresh replay, PVT, guarded publication,
final testbench validation, Maestro validation, and reports. The designer calls
the public boundary instead of copying optimizer code.

## 4. Architecture Decision

Three approaches were considered:

- **Spectre text as source:** cheap initially, but reverse parsing includes,
  expressions, defaults, model-specific terminals, and design intent is fragile.
- **Virtuoso schematic as source:** high PDK fidelity, but remote iteration is
  slow and conflicts with the requested Windows-first design loop.
- **Versioned Circuit IR with two backends:** deterministic Spectre generation
  for fast iteration, followed by Virtuoso materialization and round-trip
  verification.

The selected architecture is the third option. Spectre is an output before
handoff; Virtuoso becomes authoritative after handoff.

## 5. Skill Boundary and Package Layout

```text
smic180/skills/smic180-analog-designer/
  SKILL.md
  agents/openai.yaml
  analog_design/
    cli.py units.py spec.py ir.py validation.py workflow.py artifacts.py report.py
    topology/{base.py,registry.py,two_stage_miller.py}
    sizing/{base.py,square_law.py}
    technology/{base.py,smic180.py,discovery.py}
    netlist/{ast.py,spectre_writer.py,spectre_reader.py,equivalence.py}
    simulation/{direct_spectre.py,diagnostics.py}
    virtuoso/{plan.py,materialize.py,readback.py,export.py}
    adapters/{simulator.py,optimizer_v2.py}
  scripts/analog_design.py
  references/{design-spec-v1.md,circuit-ir-v1.md,smic180-live-discovery.md,two-stage-miller.md}
```

Tests belong under `smic180/tests/analog_design/`. No production module is
placed inside either existing skill.

## 6. Data Model

`design_spec.json` version 1 contains metadata, technology, circuit,
interfaces, operating conditions, loads, metrics, PVT, preferences, and
publication. Every metric is `hard`, `objective`, or `report`; declares units,
comparison, analysis source, and conditions; and cannot source phase margin from
ordinary AC.

`circuit_ir.json` version 1 contains:

```text
version, metadata, technology, circuit, ports, nets, instances, parameters,
matching_groups, supplies, biases, analyses, measurements, constraints,
optimization, provenance
```

Each instance records a stable ID, role, generic device class, stable technology
`master_ref`, terminal-net map, logical parameters, physical parameters, CDF
expectations, optimization references, matching groups, and rationale.
`master_ref` is resolved by a confirmed technology profile rather than storing
an unverified Cadence master directly.

Parameters have stable IDs, physical SI values and bounds, target type, linked
instances, quantization, and provenance. Requested equation values,
technology-normalized candidates, reopened CDF values, and published optimizer
values remain distinct.

## 7. SMIC180 Technology Profile

The technology profile has `unconfirmed` and `confirmed` states. An unconfirmed
profile supports offline tests only and is rejected by live materialization.

Live discovery records the resolved PDK and library mapping, device masters and
views, terminal names and order, CDF names/types/defaults/callbacks/units,
observed legal dimensions, bulk policy, model names/includes/sections/corners,
disposable-device round trips, and direct-versus-exported normalization.

The provided PDF is background evidence, but its current text extraction is
corrupted and cannot prove live library facts. Confirmed adapters cite live query
artifacts. Unknown values remain unresolved and block their live stage.

## 8. Golden Topology and Sizing

The first topology plugin emits a fixed two-stage Miller op amp: explicit NMOS
or PMOS input pair, current-mirror load, tail bias, second gain stage, Miller
capacitor, disabled optional nulling-resistor slot, and VDD/VSS/VINP/VINN/VOUT
plus explicit bias interfaces.

The initial sizing engine produces estimates rather than signoff values. Each
calculation stores formula ID, inputs, assumptions, dimensions, result, and
confidence. Version 1 implements a square-law/hand-analysis seed behind an
interface that can later accept a characterized gm/Id engine.

## 9. Deterministic Spectre Generation and Design Loop

The netlist backend uses a small AST and canonical ordering for includes,
parameters, subcircuits, instances, analyses, saves, and options. It uses stable
SI serialization and embeds the IR digest and generator version.

Supported initial analyses are DC operating point, AC, transient, and requested
noise. STB remains unverified until a real loop-break testbench is accepted.

Every iteration stores its IR, immutable deck, manifest, log, raw data,
measurements, operating points, and diagnosis. Success requires fresh finite
measurements and expected raw results, not only process exit zero. Version 1
keeps topology fixed and changes only declared sizing, bias, and compensation
parameters. Freeze requires hard-spec success or an explicitly permitted,
finite, documented near-feasible Optimizer V2 baseline.

## 10. Virtuoso Materialization

Materialization is plan-first and supports `--plan-only` and `--dry-run`. It
verifies safe identifiers, target absence, confirmed masters, terminals, and
technology adapters before mutation. Existing cells are protected and source
cells are never replacement targets.

The materializer creates instances, applies CDF values through verified
callbacks, connects with headless-safe terminal labels, saves and closes,
reopens and reads effective values/connectivity, runs `schCheck` and `dbSave`,
then exports a fresh pure circuit netlist with batch `si`. Every step writes
evidence; an in-memory create response cannot confirm the stage.

## 11. Equivalence Gate

Structural comparison parses both netlists into normalized graphs and compares
ports, device identity, terminal-net connectivity, and effective parameters. It
tolerates ordering, formatting, generated names, explicit defaults, and
technology-declared equivalent representations. It is not a line diff.

Simulation comparison runs both decks freshly under identical conditions and
compares declared DC nodes, supply currents, gain, bandwidth, and other metrics
using per-metric absolute and relative tolerances. Missing, stale, NaN, or empty
results fail. `equivalence.confirmed.json` is written only after both checks pass.

## 12. Existing-Workflow Adapters

The simulator adapter emits proposed `pin_classifications.json`,
`sim_config.json`, and testbench intent, validates them with existing loaders,
and retains engineer/LLM review for electrical polarity and stimulus correctness.

The Optimizer V2 adapter consumes frozen IR plus actual schematic/CDF evidence.
It emits a strict version-2 configuration and physical baseline whose cells are
distinct, references were observed, matching variables are linked, and bounds
have discovery evidence. Direct-deck sizes are seeds only. Optimizer V2 owns all
CDF updates, fresh replay, PVT, publication, and final-result verification.

## 13. Workflow and Artifacts

Confirmed states are:

```text
initialized -> spec_validated -> topology_selected -> initial_sizing_complete
-> ir_validated -> windows_nominal_passed -> candidate_frozen
-> schematic_created -> cdf_roundtrip_passed -> schematic_checked
-> equivalence_passed -> simulator_validated -> optimization_complete
-> pvt_passed -> published -> final_validation_passed
```

Each transition declares input artifacts, outputs, validators, and a narrow
confirmation record. Failed attempts do not advance state. Resume recomputes
hashes and revalidates the latest confirmed transition. After optimizer handoff,
the optimizer's state is authoritative and the designer stores references.

Runs use `${AMS_OUTPUT_ROOT}/analog_design/<timestamp>/` and the requested
directory structure. JSON artifacts contain schema version, generator version,
timestamps, source hashes, and provenance. `.latest_run` updates atomically only
after initialization succeeds.

## 14. Safety and Failure Policy

- Never overwrite a cell without explicit authorization or edit a source cell.
- Never infer roles from names, connectivity from screenshots, or PDK mappings
  from stale netlists or documentation alone.
- Never equate exit zero with metric success, nominal with PVT, or search best
  with fresh replay.
- Never publish without Optimizer V2 PVT and independent final-testbench gates.
- Never write confirmation markers from partial or stale evidence.

Preserve finite categorized failures for convergence, missing metrics, invalid
operating points, bridge faults, CDF mismatches, and equivalence mismatches.

## 15. CLI Surface

```text
analog_design.py validate-spec --spec PATH
analog_design.py plan --spec PATH --run-dir PATH
analog_design.py build-ir --run-dir PATH
analog_design.py render-netlist --run-dir PATH
analog_design.py simulate --run-dir PATH
analog_design.py freeze --run-dir PATH
analog_design.py discover-technology --output PATH
analog_design.py materialize --run-dir PATH [--plan-only] [--replace-target]
analog_design.py verify-equivalence --run-dir PATH
analog_design.py prepare-simulator --run-dir PATH
analog_design.py prepare-optimizer --run-dir PATH
analog_design.py resume --run-dir PATH
analog_design.py report --run-dir PATH
```

Pre-materialization commands are offline-testable with a fake unconfirmed
profile. Live commands reject unconfirmed profiles.

## 16. Testing and Milestones

Offline tests cover schemas, strict SI parsing, IR integrity, floating critical
nodes, matching groups, deterministic netlists, terminal/parameter normalization,
topology registration, sizing provenance, state/resume hashes, marker guards,
semantic netlist equivalence, metric tolerances, adapter schema validation, cell
overwrite refusal, and unconfirmed-profile refusal.

Live disposable-cell tests verify actual SMIC180 masters, terminals, callbacks,
close/reopen CDF values, `schCheck`, `si` export, direct/exported equivalence,
baseline optimizer evaluation, and source immutability.

Milestones are:

1. Offline core: schemas, units, topology registry, Miller plugin, initial sizing,
   IR validation, AST netlist generation, and unit tests.
2. Direct Spectre loop: fresh simulation, parsing, diagnostics, iteration
   artifacts, and candidate freeze.
3. PDK/Virtuoso handoff: live discovery, materialization, CDF round trip,
   `schCheck`, `si` export, and equivalence gate.
4. Existing-workflow adapters: reviewed simulator inputs, strict Optimizer V2
   configuration, baseline evaluation, and a nonpublishing trial.
5. Acceptance: full search, fresh replay, PVT, publication, independent final
   testbench verification, required Maestro verification, and final report.

Each milestone is test-first and independently verified before the next begins.

## 17. Live Information Still Required

Implementation must discover rather than guess:

- authoritative PDK root and library mappings;
- NMOS/PMOS, resistor, and capacitor masters, views, and terminals;
- CDF names, units, callbacks, quantization, and legal values;
- bulk policies and model/include/section/corner mapping;
- direct versus `si` device-line normalization;
- a reliable STB loop-break arrangement;
- nonconflicting golden op-amp library and cell names;
- whether Spectre executes locally on Windows or remotely while Windows remains
  the orchestration host.

The first live milestone must also resolve the discrepancy between the supplied
PDK root `/home/IC/Tech/smic18ee_2` and the longer path currently present in site
configuration. No code may silently select one.

## 18. Acceptance

Version 1 is complete only when a real two-stage Miller op amp proves every
requested criterion: deterministic IR-to-deck generation, fresh nominal results,
real Virtuoso creation and reopened CDF evidence, `schCheck`, fresh exported
netlist simulation, structural and metric equivalence, valid simulator handoff,
valid Optimizer V2 baseline, fresh replay and PVT, guarded publication,
independent final testbench verification, resumable audited artifacts, truthful
reports, and passing new plus existing regressions.

Until live evidence proves each claim, workflow state and reports must mark that
stage incomplete or unverified.

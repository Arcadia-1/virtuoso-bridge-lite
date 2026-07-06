# Netlist Semantic Cleanup Reference

## Mental Model

Treat a generated Spectre/SPICE deck as evidence, not as the final source
artifact. Netlist cleanup turns that evidence into files an analog designer or
agent can read, modify, simulate, and optimize without losing the original
experiment.

The cleanup engine is semantic curation over a circuit graph:

- nets are nodes;
- MOS devices, passives, sources, and subckts are typed hyperedges;
- ports, probes, saved signals, ADE expressions, and schematic labels are
  strong evidence;
- symmetry, shared gates, diode connections, feedback paths, and operating
  points are consistency checks.

Do not do blind text rewriting. Scripts can find leftover tool artifacts, but
the model or engineer must decide circuit boundaries, node meaning, instance
names, and which abstractions are real.

Keep the raw generated deck untouched. Cleanup may produce two derived
artifacts:

- **reference netlist**: readable and editable; preserves MOS parameters by
  default, but may have an explicitly stripped variant for context density or
  schematic-level reasoning. It may merge equivalent ideal passives, rename
  devices/nodes, and simplify hierarchy.
- **simulation-equivalent netlist**: numerically faithful to the raw ADE/PEX
  deck; preserves the MOS parameters, unit passives, includes, hierarchy, and
  simulator setup needed to reproduce the original run.

Use simulation-equivalent artifacts to prove that splitting and renaming did
not change behavior. Use reference artifacts for review, explanation, future
edits, and optimization. Do not optimize from a reference artifact until it has
been validated for the metrics under study.

## Artifact Roles

Separate reusable content by role:

- `raw/`: untouched ADE/PEX/generated source artifacts.
- `netlist/dut/`: DUT subckts and real DUT wrappers.
- `netlist/tb/`: supplies, clocks, stimulus, loads, probes, and helper blocks.
- `netlist/runs/`: parameters, model includes, corners, saves, and analyses.
- `netlist/va/`: Verilog-A helpers when needed.

## Boundaries and Hierarchy

First decide what is real DUT, what is testbench, and what is run setup. A DUT
file defines only subckts and internal devices. A testbench owns supplies,
clocks, stimulus, loads, probes, and helper blocks. A run file owns includes,
corners, sweep values, save directives, and analyses.

Do not add hierarchy just because a file looks cleaner with more subckts. Keep
a flat transistor-level block flat when the netlist has no real internal
subckt, repeated unit, or reusable boundary. Use comments, section ordering,
and semantic names to make a flat block readable.

Add a stable `<block>_under_test` wrapper only when it serves a real purpose:
multiple DUT variants, a reusable testbench interface, or a need to swap DUTs
without editing the testbench. A single flat design usually does not need a
wrapper.

## MOS Parameters

Do not strip MOS layout/extraction parameters by default. Keeping them is the
conservative choice because layout-derived parameters can change performance.
Use a stripped reference only when the goal is context density, agent editing,
or schematic-level reasoning, and label it as a behavior-changing
simplification that must be compared against the full-parameter deck.

A stripped MOS line keeps structural identity and drawn geometry:

```spectre
mn_input_vinp_main (sense_n VINP tail_src VSS) nch_ulvt_mac l=30n w=16u multi=1 nf=32
```

Stripped keep-list:

- `l`
- `w`
- `nf`
- `fingers`
- `m`
- `multi`

In a stripped reference, keep extra parameters only when they are part of the
schematic design contract, such as `stack`, `seg`, `nfin`, or project-local
geometry choices.

Parameters commonly removed only in stripped references:

- diffusion/perimeter terms: `ad`, `as`, `pd`, `ps`
- source/drain resistance annotations: `nrd`, `nrs`
- stress/proximity terms: `sa`, `sb`, `sca`, `scb`, `scc`, `spa`, `sap`,
  `sapb`, `spba`, `spmt`, `spomt`, `spmb`, `spomb`
- DFM flags, extraction flags, simulator-generated proximity tails
- raw PEX mesh devices unless the task is explicitly parasitic modeling

This is not a post-layout reduction method. If post-layout-equivalent behavior
matters, keep the raw/extracted deck or build a dedicated parasitic-reduction
flow.

## Semantic Inference

Start from connectivity, not instance order. Mark the strongest landmarks
first:

- supply/body nets: `VDD`, `VSS`, wells, body ties
- external signal ports: `VINP`, `VINN`, `VOUTP`, `VOUTN`, `CLK`, `RST`
- bias nets: gates driven by diode-connected devices, mirrors, or current
  references
- gain nodes: drain intersections with high small-signal resistance
- feedback/control nets: paths from outputs or common-mode sense nodes back into
  bias/control gates

Use evidence in roughly this order:

1. subckt ports, schematic labels, and explicit pin names;
2. testbench stimulus, probes, saved signals, and ADE output expressions;
3. local graph topology, symmetry, mirrors, feedback, and clock phases;
4. DC operating point, small-signal parameters, and measured waveforms;
5. analog design experience from similar circuits.

Do not promote a weak inference into a confident semantic name. If the evidence
does not identify a node or device, keep a neutral name and add a review note.

Common graph classifications:

- Differential input devices: gates on differential inputs, common source/tail,
  drains at paired high-impedance nodes.
- Tail/current source: device from the input pair's common source to a supply
  with a bias-controlled gate.
- Mirror reference/output: same-type devices with shared gates, one
  diode-connected.
- Active load: PMOS/NMOS load devices tied to gain/output nodes through mirror
  or bias gates.
- CMFB path: devices sensing output common-mode or `VOUTP/VOUTN` and driving
  bias/control nodes.
- Compensation/load/sampling/reset passives: classify by terminals and nearby
  stage, not by generated name.

Use operating-point and small-signal results to check the inference when
available:

- input devices should have meaningful `gm`;
- current sources should carry intended branch current;
- gain nodes should show high output resistance;
- semantic stages should form a continuous input-to-output path;
- CMFB devices should control common-mode or bias, not be mislabeled as forward
  gain devices.

## Naming

Name meaningful nodes and instances by function, polarity, and stage. Examples:

```spectre
mn_stage1_in_vinp
mn_stage1_in_vinn
mn_stage1_tail
mp_stage1_load_mirror_ref
mp_stage1_load_mirror_out
mn_cmfb_sense_voutp
mn_cmfb_sense_voutn
mp_stage2_load
c_comp_stage2
```

Good evidence for renaming:

- subckt ports and pin names
- schematic labels and saved signals
- ADE output expressions and probes
- graph symmetry and mirror topology
- DC/AC/noise operating-point evidence

Weak evidence:

- original instance order such as `M0`, `M1`
- physical order in the generated netlist
- a single neighboring net name without topology support
- assumptions from a similar but different circuit

If meaning is ambiguous, keep a neutral name and add a review note. Do not
invent certainty.

Check differential symmetry after renaming. If names imply a differential pair,
mirror load, cross-coupled latch, or CMFB sense pair, the two sides should have
matching model types, dimensions, and connection patterns unless the asymmetry
is intentional and documented.

After a rename batch, search for stale names in netlists, includes,
testbenches, save/probe statements, measurement expressions, evaluator scripts,
and README text. Many cleanup failures are caused by helper scripts still
reading old hierarchy or signal names.

## Passive Consolidation

In reference decks, parallel ideal passives may be merged when they have
identical terminals and equivalent model semantics.

Do not merge passives in simulation-equivalent decks unless numerical
abstraction is explicitly allowed. Avoid merging when:

- passives are PDK devices with geometry, voltage dependence, mismatch, or noise
  behavior;
- instances are named, probed, trimmed, or calibrated;
- the split encodes layout matching or unit weighting;
- the task is parasitic reduction rather than semantic cleanup.

## Semantic Groups and Optimization

When the cleaned deck may be resized or optimized, semantic naming should also
produce group-level knobs. Common groups:

- `input_stage`: input pair and input devices that set input `gm`, noise, and
  first-stage pole.
- `gain_stage` or `stage<N>_gain`: devices providing forward gain at
  high-impedance nodes.
- `source` or `current_steering`: tails, source-side devices, current steering,
  and branch-current controls.
- `output_driver`: devices directly driving external output or probe nodes.
- `output_p` / `output_n`: PMOS/NMOS output halves, separated when common-mode
  or pull-up/pull-down balance matters.
- `cmfb_core`: common-mode sensing and correction devices.
- `bias_main`: mirrors, diode references, and bias distribution for the main
  signal path.
- `sampling_switch`, `reset`, `precharge`, `clock_buffer`: clocked auxiliary
  networks; keep fixed until a concrete timing, reset, or charge-injection
  issue is measured.
- `dummy`, `tie`, `off_device`: layout balance, tie-off, and inactive devices;
  usually exclude from first-pass optimization.

Record the grouping map in a generator script, manifest, or review note. Groups
should preserve intended symmetry across differential halves and polarities.

Prefer group-level actions before exposing every MOS independently:

- set a group to a legal channel length;
- multiply group `multi` to restore `gm` or drive after length changes;
- scale `output_p` and `output_n` separately to adjust output common-mode;
- scale real bias currents or bias mirrors when additional power is allowed;
- make small bounded CMFB-strength changes when common-mode metrics justify it.

Protect experiment invariants as hard constraints:

- feedback/sampling capacitor ratios;
- external loads, probes, and measurement nodes;
- supply voltages, common-mode references, stimulus amplitudes, clock timing,
  and analysis statements;
- model includes, process corner, simulator preset, and temperature;
- topology-defining connectivity.

Do not let an optimizer improve metrics by changing the experiment. Changing a
feedback ratio, hiding load, moving probes, altering stimulus, or adding ideal
helper sources is a specification change, not ordinary sizing.

Use a constraints-first objective: bandwidth/GBW, phase margin, output
common-mode, settling, power, noise, and offset usually outrank secondary gain
or score terms. Inspect Pareto candidates, not only the lowest scalar score,
and save a manifest with group edits, protected invariants, simulator command,
raw-result path, and extracted metrics.

## Validation

Use the strongest feasible validation:

1. Syntax or parser check for every generated deck.
2. Spectre DC smoke run.
3. AC/noise/transient comparison against raw or ADE-generated deck when
   numerical equivalence is claimed.
4. Per-device operating-point comparison for renamed critical devices.

Run deterministic checkers after the semantic pass, not before it. Treat checker
findings as prompts for review: leftover random names, DUT/testbench mixing, or
unexpected stripped parameters still need circuit judgment.

For simulation-equivalent cleanup, compare actual metrics, not just successful
completion:

- supply current and power
- DC output and common-mode voltages
- gain, bandwidth, GBW, and stability metrics
- output and input-referred noise
- representative transient amplitude, delay, and settling
- key device `gm`, `gds`, `cgg`, `gm/Id`, region, and self-gain

Use the raw deck as baseline and report the evaluation convention. For percent
delta, use `(variant - baseline) / baseline * 100%` and name the baseline
explicitly. Sparse transient samples can hide ripple or create false ringing
impressions; rerun a dense-strobe window before making settling or ringing
claims.

When comparing standalone Spectre to ADE/Maestro:

- start from the exact per-point `input.scs` and included `netlist`;
- preserve model sections, design parameters, analyses, saves, simulator mode,
  and corners;
- if changing output format for parsing, keep other simulator options aligned;
- compare point by point and state interpolation conventions for computed
  metrics such as bandwidth or GBW.

Large differences usually mean the standalone deck is not the same experiment
as the ADE point deck.

For Maestro or ADE-driven sweeps, verify the final generated Spectre deck for
each point. GUI variables and CDF parameters are not proof that the simulator
saw the intended values. Inspect the per-point netlist and confirm that sizing,
`multi`/`m`/`simM`, bias currents, model sections, and fixed variables actually
landed in the run deck.

## Checklist

1. Archive the raw generated netlist under `raw/`.
2. Identify DUT boundary, testbench scope, and run-deck scope.
3. Decide whether both reference and simulation-equivalent artifacts are needed.
4. Build semantic rename maps from ports, probes, labels, graph structure, and
   operating-point evidence.
5. Split files into `dut/`, `tb/`, `runs/`, and optional `va/`.
6. Preserve MOS tails by default; create a stripped variant only when explicitly
   useful and validate the performance delta.
7. Consolidate passives only when allowed by artifact type and model semantics.
8. Record unresolved semantic questions as review notes.
9. Record semantic groups if later sizing or optimization is likely.
10. Run checker, syntax/smoke validation, and metric comparison as appropriate.

## Example

Generated MOS line:

```spectre
M21 (VO1P VINPN VBN VSS) nch_ulvt_mac l=30n w=2u multi=1 nf=8 sd=100n ad=1e-13 as=1.125e-13 pd=2.8u ps=3.4u nrd=0.273611 nrs=0.273611 sa=295.835n sb=295.835n spmt=1.11111e+15 dfm_flag=0
```

Clean reference line:

```spectre
mn_input_vinp_stage1 (vo1_p vinp_n vbias_n VSS) nch_ulvt_mac l=30n w=2u multi=1 nf=8
```

The second line is not just parameter pruning: names encode circuit function.

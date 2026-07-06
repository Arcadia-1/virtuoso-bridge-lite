---
name: netlist
description: "Semantic cleanup and curation of Spectre/SPICE transistor netlists for analog review or reusable reference circuits. Use when Codex needs to understand generated ADE/PEX netlists, split DUT/testbench/run decks, optionally strip layout-derived MOS tail parameters by reasoning, rename random nodes/instances into semantic names, or prepare clean comparator/opamp/SAR reference netlists. Scripts in this skill are checkers only, not primary cleanup engines."
---

# Netlist Semantic Cleanup

Use this skill when converting generated netlists into readable, reviewable
Spectre/SPICE reference decks.

## Principle

The primary cleanup engine is semantic understanding by the model and engineer.
Do not treat netlist cleanup as a blind text rewrite. A checker can report
leftover tool artifacts, but it cannot decide circuit boundaries, node meaning,
or instance names.

## Workflow

1. Preserve the raw generated artifact. Never edit ADE/PEX output in place.
2. Read enough context to understand the circuit: ports, hierarchy, stimulus,
   measurements, clocking, biasing, and intended DUT boundary.
3. Read `references/cleaning.md` before curating a real design.
4. Draft the semantic cleanup plan:
   - DUT subckt or wrapper interface.
   - Testbench-owned supplies, clocks, stimulus, loads, probes, and analyses.
   - Run-deck-owned parameters, model includes, corners, sweeps, and saves.
   - Rename map for random nodes/instances, plus unresolved review notes.
5. Create the clean netlist as a curated source artifact. Use model reasoning to
   split DUT/testbench/run decks and to choose semantic names.
6. Run the checker only after the semantic pass:

```bash
python skills/netlist/scripts/check_spectre_netlist.py netlist/dut/block.scs --mode dut
python skills/netlist/scripts/check_spectre_netlist.py netlist/tb/tb_block.scs --mode tb
```

The checker reports suspicious MOS tail parameters, random-looking node names,
generic instance names, and DUT/testbench mixing. It does not rewrite files and
should not be used as the main cleanup mechanism.

## Rules

- Preserve MOS instance parameters by default when numerical behavior matters.
  Removing layout side-effect tails is an explicit stripped-reference choice,
  not the default.
- If a stripped reference is requested, it may remove `ad/as`, `pd/ps`,
  `nrd/nrs`, `sa/sb/sca/scb`, `sp*`, DFM, stress, proximity, and extraction
  parameters, but expect performance differences and validate against the
  full-parameter or raw deck.
- Split reusable artifacts into DUT, testbench, and run decks. A clean DUT file
  should not contain supplies, clocks, sweeps, saves, probes, or analysis
  statements.
- Replace random names (`net1`, `_net23`, `N_*`, numeric mesh nodes, `I42`,
  `M0`) with semantic names where the circuit meaning is known. If meaning is
  unknown, leave a short review note instead of guessing.
- Validate after cleanup with a parser or Spectre syntax/smoke run when
  Cadence is available.

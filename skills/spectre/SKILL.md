---
name: spectre
description: "Run Cadence Spectre simulations remotely via virtuoso-bridge: upload netlists, execute, parse PSF results. TRIGGER when the user wants to run a SPICE/Spectre simulation from a netlist file, do transient/AC/PSS/pnoise analysis outside Virtuoso GUI, parse PSF waveform data, run multiple simulations in parallel across one or more servers, check simulation job status, or mentions Spectre APS/AXS modes. Also triggers for sim-jobs, sim-cancel, or parallel/concurrent simulation requests. Use this for standalone netlist-driven simulation — for GUI-based ADE Maestro simulation, use the virtuoso skill instead."
---

# Spectre Skill

Run a `.scs` netlist locally or on a remote machine through SSH, then parse PSF results into Python dicts. Independent of VirtuosoClient — no GUI needed.

## Before you start

1. **`virtuoso-bridge` is a Python CLI** — install it in a virtual environment with `uv pip install -e virtuoso-bridge-lite`.
2. `virtuoso-bridge status` — check connection, Spectre path, license
3. Check `examples/02_spectre/` — use existing examples as a basis
4. `spectre` must be on `PATH`, or set `VB_CADENCE_CSHRC` (project or user `.env`) so the runner can source the Cadence environment. This applies to local and SSH execution.

## Core pattern

```python
from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

sim = SpectreSimulator.from_env(
    spectre_args=spectre_mode_args("ax"),  # APS extended (recommended)
    work_dir="./output",
)
result = sim.run_simulation("my_netlist.scs", {})

if result.ok:
    vout = result.data["VOUT"]
else:
    print(result.errors)
```

With Verilog-A includes:
```python
result = sim.run_simulation(
    "tb_adc.scs",
    {"include_files": ["adc.va", "dac.va"], "spectre_args": ["+aps"]},
)
```

`include_files` and per-run `spectre_args` have the same meaning in local and
SSH mode. They stage include files and extend the command; they do not inject
arbitrary circuit parameters. For local-only execution, use
`SpectreSimulator.local(...)`, or configure `VB_REMOTE_HOST=localhost` and use
`from_env()`.

## Result object

| Attribute | Content |
|-----------|---------|
| `result.ok` | Whether simulation succeeded |
| `result.data` | Parsed waveforms plus scalar OP values; STRUCT OP entries are flattened as `"instance:parameter"` (for example `"M0:gm"`) |
| `result.errors` | Error messages (short, classified); check these whenever `result.ok` is false |
| `result.metadata["timings"]` | Execution and parse durations, plus transfer timing in SSH mode |
| `result.metadata["output_dir"]` | Local path to `.raw` directory |

Treat `result.ok` as the execution contract. A nonzero exit, explicit fatal
Spectre output, netlist read-in error, or explicit convergence failure returns
`FAILURE`/`PARTIAL` even if the raw directory contains incomplete files. Do not
use a non-empty `result.data` as proof that the simulation succeeded.

## Gotchas (Spectre 21.1 + IC618 lab cluster)

These are silent or near-silent foot-guns from real lab runs:

- **`-param X=Y` CLI flag is BROKEN.** Spectre 21.1 parses the value as a
  second input netlist → `SPECTRE-132: input file has been re-specified as 'X=Y'`.
  **Workaround**: bake parameters into the netlist (regenerate the master per
  sweep point with `txt.replace("parameters X=0", f"parameters X={val}")`).
- **`parameters X=Y` re-declaration after `include "header.scs"` does not
  update DEPENDENT expressions.** E.g., header has `parameters N=64 t_end=((N+N_extra)/Fs)`,
  then later `parameters N=256` — N updates but `t_end` stays at 276 ns
  (eagerly bound from the first declaration). Symptom: tran stops far too
  early. **Fix**: copy header locally and edit the `parameters` line in place.
- **Default `timeout=600 s` is too short for noised long-tran**. With
  `tranNoise=yes` + N≥256 or 6+-way parallel contention, a single run can
  exceed 600 s wall while spectre is still progressing — bridge reports
  "Remote command timed out" but spectre.out actually shows clean completion.
  **Fix**: `SpectreSimulator.from_env(timeout=3600, ...)`.
- **PSF parser keeps `\<>` escape chars in signal names.** Saved signal
  `DOUT\<0\>` parses as dict key `r"DOUT\<0\>"`, not `"DOUT<0>"`. Symptom:
  `KeyError: 'DOUT<0>'` even though save list looks right.
- **`strobeoutput=all` in psfascii outputs only the continuous tran.** Despite
  the docs implying "both continuous + strobed", Spectre 21.1's psfascii
  emitter writes just the continuous stream into `tran.tran.tran`. You'll get
  ~140k samples per signal instead of N strobed values. **Fix**: either
  Python-strobe yourself with `np.searchsorted(t, k/Fs + offset)`, or use
  `strobeoutput=strobeonly` (which DOES work and shrinks the PSF ~1500×).

## Parallel simulation

Submit simulations that run concurrently. Each task gets a unique
`<netlist-stem>__<run-id>/` directory below `work_dir`, plus its own remote
directory when applicable, so even repeated submissions of the same deck do
not overwrite PSF data or auxiliary files. For full API and multi-server
setup, read `references/parallel.md`.

```python
t1 = sim.submit(Path("tb_comp.scs"))    # returns Future immediately
t2 = sim.submit(Path("tb_dac.scs"))     # submit more anytime
result = t1.result()                     # block on one
results = SpectreSimulator.wait_all([t1, t2])  # or wait for batch
```

## Simulation modes

Precision ordering (measured on an 11-bit sub-radix-2 SAR ADC tran, N=128
coherent FFT, ax baseline ≈ 220 s):

| arg | preset | speed | ENOB Δ vs `aps` | use for |
|---|---|---|---|---|
| `"spectre"` | (none) | slowest | reference | least license demand, basic direct |
| `"aps"` | `+preset=aps` | 1.0× (gold) | 0.000 | sign-off accuracy reference |
| `"cx"` | `+preset=cx` | 1.2× | −0.03 | sign-off for designs with mixed-signal stiff loops (cmp metastability) |
| `"ax"` | `+preset=ax` | **2.0×** | **−0.03** (within noise) | **default for daily work** |
| `"mx"` | `+preset=mx` | 3.8× | −0.29 | design exploration, corner sweeps where 0.3 ENOB is acceptable |
| `"lx"` | `+preset=lx` | 5.9× | −2.8 (**unusable for SAR**) | small-signal AC / linear DC sweeps; not for circuits with cmp/regen |
| `"vx"` | `+preset=vx` | 8.8× | −8.5 (**totally fails**) | verification-style connectivity / DC convergence only — never for transient signal fidelity |

```python
spectre_mode_args("ax")     # default for daily transient work
spectre_mode_args("aps")    # reference / sign-off
spectre_mode_args("mx")     # fast iteration if ENOB ≤ 0.3 loss is OK
```

Critical: SAR / latched-comparator circuits and any topology with
metastable regeneration depend on tight `reltol` (1e-4 or better) to
resolve LSB-scale differential inputs. `lx` relaxes `reltol` to ~1e-3
and drops ENOB by ~3 bits on such circuits; `vx` disables LTE bounding
entirely and produces garbage. Reserve those two for non-signal-fidelity
work (DC, connectivity, link-test).

If a Maestro config you inherit specifies `+preset=lx` or `+preset=vx`
for a transient performance sim, that's almost always a bug.

## When (and when not) to replace cells with Verilog-A for speedup

Verilog-A behavioral replacement of cells is a tempting acceleration lever, but
the speedup is **non-monotonic in cell size** — replacing big cells helps,
replacing small cells **hurts**.  Measured on a 11-bit SAR ADC tran (ax mode,
N=64, baseline 132s):

| Cell replaced | Transistor count | Wall-time change | Result |
|---|---|---|---|
| Output capture DFFs (1-pin behavior, 12 instances × 1 D-FF each) | 12 × ~10 MOS | 0% (neutral) | ✓ Easy, no gain — skip unless cleaning the netlist |
| Per-bit SAR latch with feedback (12 × ~12 MOS + 4 std cells) | ~200 MOS total | **−13% (slower)** | ✗ `transition()` event-queue overhead × 11 concurrent instances exceeds the BSIM equation savings |
| StrongARM comparator (47 MOS) | 47 MOS, 1 instance | **+9-17%** | ✓ Big cell, single instance — clear win |

**Rule of thumb**: VA replacement helps when the cell is **large** (≥ 40 MOS)
and instantiated **once or twice**.  It hurts when the cell is **small** (< 20
MOS) and **many instances** share the same input event source — each `@(cross())`
adds to the spectre event queue; with N concurrent instances watching the same
node, queue overhead grows ~N× while the BSIM savings stay linear in N.

**Self-timed feedback loops are extra-fragile**: replacing one element of an
async chain (e.g., a SAR daisy-chain latch with feedback to CMPCK) requires
matching not just the steady-state truth table but the propagation delay and
edge timing to within a few ps.  Standalone unit-test the VA before integrating
into the chain; if the unit-test passes but the chain breaks, suspect
`transition()` `td` interacting with multiple concurrent listeners.

**The actually-effective SAR speed levers** (measured, not from VA):

| Lever | Mechanism | Typical speedup | ENOB cost |
|---|---|---|---|
| Cut FFT N (e.g., 128 → 64) | Tran stop time scales linearly | ~40% | 0 (within meas noise) |
| `strobeoutput=strobeonly` + lean save | Cuts download + parse overhead; file size 1000× smaller | ~5-10% wall, 1500× disk | 0 |
| Replace 1-2 big cells (cmp / opamp) with VA | Skip BSIM equations for ~50+ MOS | ~10-20% | depends on VA fidelity |
| Drop LPE std-cell models for schematic-spi | Remove per-cell wire parasitics | ~20% | minor timing shift |
| Increase `maxstep` | Fewer solver iterations | ~20% per 2× | depends on circuit, risky for cmp metastability |
| Spectre mode `ax → mx` | Looser solver tolerance | ~50% | −0.3 ENOB on SAR |

The first four stack without ENOB cost.  The last two trade accuracy for speed.

## Output size control: save list, strobing, format

By default the `.scs` netlist's `tran tran ...` directive saves at every solver
timestep for every signal — a clocked SAR-style transient at `maxstep=5p` over
hundreds of `ns` produces 100+ MB of PSF ASCII per signal group.  Three knobs:

### 1. `saveOptions options save=<mode>` + explicit `save` list

```scs
save CLKS RSTP I_SAR.VTOPP DOUT\<11\> ... DOUT\<0\>
saveOptions options save=selected
```

- `save=allpub` — every public node + every terminal current (huge default).
- `save=selected` — **only** the nodes/terminals in the explicit `save` line.
- `save=lvlpub` — pub down to a given hierarchy level.

For production runs of large mixed-signal designs, **always use `save=selected`**
with a curated 10-20 signal list.  `save=allpub` is the most common cause of
runaway PSF size on lab-cluster sims.

### 2. `strobeoutput=<mode>` (gotcha: "all" is bigger, not smaller)

The `tran tran ...` directive accepts `strobeperiod` and `strobeoutput`:

```scs
tran tran stop=t_end maxstep=5p \
    strobeperiod=1/Fs strobeoutput=strobeonly ...
```

| Mode | What gets saved | Use for |
|---|---|---|
| `strobeoutput=all` | **Every solver timestep PLUS strobed samples** (biggest file) | Debugging — need waveform shape between samples |
| `strobeoutput=strobeonly` | **Only** strobed samples (1 sample per `strobeperiod`) | ENOB / SNDR / corner sweeps where you only need per-cycle values |

The name "all" misleads — it means "both continuous and strobed views," not
"all signals."  Switching to `strobeonly` typically cuts file size 500×-1500×
on N=64..256 sims.  **For ENOB-only runs of a clocked ADC**, `strobeonly` is
the right default.

### 3. `output_format` — PSF ASCII vs binary

The bridge currently uses `output_format="psfascii"` by default, parsed via
`parse_spectre_psf_ascii`.  **`output_format="psfbin"` is NOT supported by the
in-tree parser** (`virtuoso_bridge/spectre/parsers.py` has no
`parse_spectre_psf_bin`).  Passing it will produce a `.raw` directory the local
side cannot read.

If you need 10× smaller PSF files: add a binary parser (e.g., wrap
`psf_utils` — pure Python, pip install).  Until then, the size lever is
`save=selected` + `strobeoutput=strobeonly`, not the format.

## Transient noise (`tranNoise=yes`)

`tran tran` is **deterministic by default** — no thermal / 1/f noise injected.
Most BSIM models have noise params but they only fire during `noise` analysis
or when `tranNoise=yes` is on the `tran` line:

```scs
tran tran stop=t_end maxstep=5p \
    tranNoise=yes noisefmax=50G noiseseed=1 noisetmin=1 binnum=16 noiseruns=1 \
    write="spectre.ic" writefinal="spectre.fc" annotate=status
```

| Param | Meaning | Default-ish value |
|---|---|---|
| `tranNoise=yes` | Enable the noise injection at all | off |
| `noisefmax=<f>` | Max frequency for noise integration | 5×Fclock or 1× signal BW (smaller = faster) |
| `noiseseed=<n>` | RNG seed for one run | 1 |
| `noisetmin=<t>` | Earliest time when noise becomes active | 0 (or 1×Ts to skip startup) |
| `binnum=<n>` | Frequency-bin discretization (Wiener model) | 16 |
| `noiseruns=<n>` | **Stochastic Monte Carlo runs** — N seeds, ensemble output | **1** (Maestro defaults to **100**, which is **100× compute**) |

**Gotcha**: When inheriting a `tran` line from Maestro, `noiseruns=100` is
common.  That makes spectre repeat the full transient 100 times with different
noise seeds for ensemble statistics — fine for jitter histograms / phase noise
analyses, but **lethal for ENOB measurement** (which only needs one
realization).  Override to `noiseruns=1` unless you genuinely want ensemble.

ENOB cost of enabling noise on a 11-bit SAR: roughly −0.3 to −0.5 bit
(strongarm cmp noise is the dominant source).  Compute cost of
`tranNoise=yes noiseruns=1` is ~1.5-2× a noiseless tran.

## References

Load when needed — these contain detailed API docs:

- `references/netlist_syntax.md` — Spectre netlist format, analysis statements, parameterization
- `references/parallel.md` — Parallel simulation, multi-server, CLI job management, .env configuration

## Examples

- `examples/02_spectre/01_inverter_tran.py` — inverter transient
- `examples/02_spectre/01_veriloga_adc_dac.py` — 4-bit ADC/DAC with Verilog-A
- `examples/02_spectre/02_cap_dc_ac.py` — capacitor DC + AC
- `examples/02_spectre/04_strongarm_pss_pnoise.py` — StrongArm PSS + Pnoise

## Related skills

- **virtuoso** — GUI-based Virtuoso workflow (schematic/layout, ADE Maestro). Use when working inside Virtuoso GUI.

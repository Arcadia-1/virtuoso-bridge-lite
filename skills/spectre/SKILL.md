---
name: spectre
description: "Run Cadence Spectre simulations remotely via virtuoso-bridge: upload netlists, execute, parse PSF results. TRIGGER when the user wants to run a SPICE/Spectre simulation from a netlist file, do transient/AC/PSS/pnoise analysis outside Virtuoso GUI, parse PSF waveform data, run multiple simulations in parallel across one or more servers, check simulation job status, or mentions Spectre APS/AXS modes. Also triggers for sim-jobs, sim-cancel, or parallel/concurrent simulation requests. Use this for standalone netlist-driven simulation — for GUI-based ADE Maestro simulation, use the virtuoso skill instead."
---

# Spectre Skill

Upload a `.scs` netlist to a remote machine via SSH, run Spectre, download and parse PSF results into Python dicts. Independent of VirtuosoClient — no GUI needed.

## Before you start

1. **`virtuoso-bridge` is a Python CLI** — installed via `pip install -e virtuoso-bridge-lite`.
2. `virtuoso-bridge status` — check connection, Spectre path, license
3. Check `examples/02_spectre/` — use existing examples as a basis
4. `.env` must have `VB_CADENCE_CSHRC` set (can live in project root or virtuoso-bridge-lite dir)

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
result = sim.run_simulation("tb_adc.scs", {"include_files": ["adc.va", "dac.va"]})
```

## Result object

| Attribute | Content |
|-----------|---------|
| `result.ok` | Whether simulation succeeded |
| `result.data` | `{"signal_name": [float, ...]}` parsed waveforms |
| `result.errors` | Error messages (short, classified) |
| `result.metadata["timings"]` | Upload, exec, download, parse durations |
| `result.metadata["output_dir"]` | Local path to `.raw` directory |

## Parallel simulation

Submit simulations that run concurrently — each gets its own remote directory, no conflicts. For full API and multi-server setup, read `references/parallel.md`.

```python
t1 = sim.submit(Path("tb_comp.scs"))    # returns Future immediately
t2 = sim.submit(Path("tb_dac.scs"))     # submit more anytime
result = t1.result()                     # block on one
results = SpectreSimulator.wait_all([t1, t2])  # or wait for batch
```

## Simulation modes

```python
spectre_mode_args("spectre")  # basic (least license demand)
spectre_mode_args("aps")      # APS
spectre_mode_args("ax")       # APS extended (recommended)
spectre_mode_args("cx")       # Spectre X custom
```

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

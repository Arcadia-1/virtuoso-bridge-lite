---
name: spectre
description: "Run Cadence Spectre simulations remotely via virtuoso-bridge: upload netlists, execute, parse results."
---

# Spectre Skill

## Running a simulation

```python
from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

sim = SpectreSimulator.from_env(
    spectre_args=spectre_mode_args("ax"),  # APS extended mode
    work_dir="./output",
    output_format="psfascii",
)
result = sim.run_simulation("my_netlist.scs", {})
```

Requires `VB_CADENCE_CSHRC` in `.env` to source the Cadence environment on the remote machine.

## Netlist: transient analysis

```spectre
simulator lang=spectre
global 0
include "/path/to/pdk/models/spectre/toplevel.scs" section=TOP_TT

// ... circuit definition ...

tran tran stop=3n errpreset=conservative
save VIN VOUT
saveOptions options save=selected
```

Result signals: `time`, `VIN`, `VOUT`, etc.

## Netlist: PSS (periodic steady-state)

```spectre
pss pss fund=1G harms=10 errpreset=conservative autotstab=yes saveinit=yes
save CLK VINP VINN DCMPP DCMPN LP LM V0:p
saveOptions options save=selected
```

Result signals: `time`, `CLK`, `VINP`, `VINN`, `DCMPP`, `DCMPN`, `LP`, `LM`, `V0:p`

PSS results are in `<raw_dir>/pss.td.pss` (time-domain, one steady-state period).

## Netlist: Pnoise (periodic noise)

Pnoise must follow a PSS analysis:

```spectre
pss pss fund=1G harms=10 errpreset=conservative autotstab=yes saveinit=yes

pnoise pnoise start=0 stop=500M pnoisemethod=fullspectrum \
    noisetype=sampled measurement=[pm0]
pm0 jitterevent trigger=[I4.LP I4.LM] triggerthresh=50m triggernum=1 \
    triggerdir=rise target=[I4.LP I4.LM]
```

Result signals: `freq`, `out` (noise spectral density in V/sqrt(Hz)).

Pnoise results are in `<raw_dir>/pnoiseMpm0.0.sample.pnoise`.

## Reading results

### From the result object (automatic parsing)

```python
result = sim.run_simulation("tb.scs", {})

# Status
result.status        # ExecutionStatus.SUCCESS / FAILURE / ERROR
result.ok            # bool
result.errors        # list of error strings
result.warnings      # list of warning strings

# Waveform data (dict: signal_name → list of float)
result.data["time"]
result.data["VOUT"]
result.data.keys()   # all available signals

# Metadata
result.metadata["timings"]      # upload, exec, download, parse durations
result.metadata["output_dir"]   # local path to downloaded .raw directory
result.metadata["output_files"] # list of PSF files
```

### Parsing PSF files directly

```python
from virtuoso_bridge.spectre.parsers import parse_psf_ascii_directory

data = parse_psf_ascii_directory("output/tb.raw")
# data = {"time": [...], "VOUT": [...], "VIN": [...]}
```

Or parse a single PSF file:

```python
from virtuoso_bridge.spectre.parsers import parse_spectre_psf_ascii

result = parse_spectre_psf_ascii("output/tb.raw/pss.td.pss")
# result.data = {"time": [...], "CLK": [...], "DCMPP": [...]}
```

### Include files (Verilog-A)

```python
result = sim.run_simulation("tb_adc.scs", {
    "include_files": ["adc_ideal.va", "dac_ideal.va"],
})
```

## Check license before running

```python
info = sim.check_license()
print(info["spectre_path"])  # which spectre binary
print(info["version"])       # version string
print(info["licenses"])      # license feature availability
```

Or via CLI: `virtuoso-bridge status` shows Spectre license info.

## Simulation modes

```python
spectre_mode_args("spectre")  # basic Spectre (least license demand)
spectre_mode_args("aps")      # APS
spectre_mode_args("ax")       # APS extended (recommended)
spectre_mode_args("cx")       # Spectre X custom
```

## Examples

- `examples/02_spectre/01_veriloga_adc_dac.py` — 4-bit ADC/DAC transient with Verilog-A
- `examples/02_spectre/04_strongarm_pss_pnoise.py` — StrongArm comparator PSS + Pnoise
- Netlists in `examples/02_spectre/assets/`

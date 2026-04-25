# Spectre Netlist Syntax Reference

## Basic structure

Every Spectre netlist follows this skeleton:

```spectre
simulator lang=spectre
global 0
include "/path/to/pdk/models/spectre/toplevel.scs" section=TOP_TT

// Circuit definition (subcircuits, instances, sources)
// ...

// Analysis statements
// ...

// Save statements
save VIN VOUT
saveOptions options save=selected
```

`save` + `saveOptions save=selected` limits output to listed signals — avoids huge PSF files.

## Analysis types

### Transient

```spectre
tran tran stop=3n errpreset=conservative
```

Result signals: `time`, plus whatever you `save`. Results in `<raw_dir>/tran.tran`.

### AC

```spectre
ac ac start=1 stop=10G dec=20
```

Result signals: `freq`, plus saved signals (complex). Results in `<raw_dir>/ac.ac`.

### PSS (periodic steady-state)

```spectre
pss pss fund=1G harms=10 errpreset=conservative autotstab=yes saveinit=yes
```

Result signals: `time`, plus saved signals (one steady-state period). Results in `<raw_dir>/pss.td.pss`.

### Pnoise (periodic noise)

Must follow a PSS analysis:

```spectre
pss pss fund=1G harms=10 errpreset=conservative autotstab=yes saveinit=yes

pnoise pnoise start=0 stop=500M pnoisemethod=fullspectrum \
    noisetype=sampled measurement=[pm0]
pm0 jitterevent trigger=[I4.LP I4.LM] triggerthresh=50m triggernum=1 \
    triggerdir=rise target=[I4.LP I4.LM]
```

Result signals: `freq`, `out` (noise spectral density in V/sqrt(Hz)). Results in `<raw_dir>/pnoiseMpm0.0.sample.pnoise`.

## Instance syntax

```spectre
// Passive devices
R0 (VIN VOUT) resistor r=1k
C0 (VOUT 0) capacitor c=10p

// Voltage source
V0 (VIN 0) vsource type=dc dc=1.8

// Subcircuit instance
XI0 (IN OUT VDD VSS) my_subckt param1=value1
```

## Common parameters

| Parameter | Meaning |
|-----------|---------|
| `errpreset=conservative` | Tighter convergence tolerances |
| `autotstab=yes` | Auto timestep stabilization (PSS) |
| `saveinit=yes` | Save initial conditions |
| `maxiters=200` | Increase max iterations for convergence |

## Netlist parameterization

Use `@@PARAM@@` placeholders in a template netlist, then replace with Python `str.replace()`:

```spectre
// In template
XI0 (IN OUT VDD VSS) nch w=@@W_INP@@ l=60n
```

```python
template = template.replace("@@W_INP@@", f"{value:.6g}")
```

# IO Ring Simulation Configuration Rules - SMIC180

Rules for generating a simulation deck configuration for IO Ring testbenches.
The LLM reads this file plus `pin_classifications.json` and produces `sim_config.json`.

**Key principle: the LLM decides WHAT to measure; the code decides HOW to express it
in Maestro OCEAN syntax.** Never write OCEAN expressions, eval_type, or save_signals
directly - specify measurement *intent* and let the code template generate correct syntax.

---

## Core Rules (Non-Negotiable)

1. **No design variables.** Never emit `design_vars` or `parameters`. All voltage/current
   values come directly from `pin_classifications.json` stimulus params - they are already
   fixed numbers.

2. **AC analysis for analog designs.** IO Ring cells do not need AC. OpAmp/LNA/Mixer/VCO require AC analysis for gain/bandwidth/phase measurements. See Section 2.

3. **Always run both DC and transient.** DC first (sets operating point), transient second.

4. **Power is always calculated from transient.** The code auto-generates the correct
   OCEAN expression when you specify `measures: ["power"]` for a pin.

5. **Do not specify model includes.** Leave `model_includes: []`. testbench_build injects them
   automatically from `_local/site.yaml`.

6. **Do not specify save_signals.** The code auto-determines the correct save level.

---

## Analysis Order and Settings

### 1. DC Operating Point

- No sweep parameter. Just an operating point.
- Runs first to establish initial conditions for transient.

### 2. Transient

- `stop`: long enough to see at least 10 full cycles of the slowest `vpulse` stimulus.
  Compute from stimulus params: `tstop = 10 * max(per)` across all `vpulse` sources.
  Minimum: `100n`. Maximum: `10u`. Round to a clean value (e.g., `500n`, `1u`).
- `errpreset=moderate` for digital-dominant cells; `errpreset=conservative` for analog-dominant.
- `maxstep`: set to `tstop / 1000`.

---

## Pin Measurements (Core Output)

Instead of writing OCEAN expressions, specify **measurement intent** per pin.

### What to specify

| Measure | What it produces | When to use |
|---------|-----------------|-------------|
| `"voltage"` | Voltage net waveform | All non-ground pins |
| `"current"` | Average current through SRC_ device | Power supply pins |
| `"power"` | Average power (VxI) through SRC_ device | Power supply pins |
| `"custom"` | User-supplied expression | Special measurements |

### Spec constraints

| Spec key | Meaning | Example |
|----------|---------|---------|
| `"i_max"` | Current must be < this value (amps) | `"0.1"` (100 mA) |
| `"p_max"` | Power must be < this value (watts) | `"0.5"` |
| `"vmax_above"` | Peak voltage must be > this (supports `*VDD`) | `"0.9*VDD"` |
| `"vmin_below"` | Minimum voltage must be < this (supports `*VDD`) | `"0.1*VDD"` |

---

## sim_config.json Schema (Produced by LLM)

```json
{
  "analyses": [
    {
      "name": "dc",
      "enabled": true
    },
    {
      "name": "tran",
      "enabled": true,
      "stop": "<computed tstop, e.g. 200n>",
      "maxstep": "<tstop/1000, e.g. 200p>",
      "errpreset": "moderate"
    }
  ],
  "model_includes": [],
  "save_default": "allpub",
  "pin_measurements": {
    "AVDD": {
      "measures": ["voltage", "current", "power"],
      "spec": {"p_max": "0.5"}
    },
    "VIOLD": {
      "measures": ["voltage", "current", "power"],
      "spec": {"i_max": "0.1"}
    },
    "EN": {
      "measures": ["voltage"],
      "spec": {}
    },
    "PG": {
      "measures": ["voltage"],
      "spec": {}
    }
  }
}
```

---

## SMIC180-Specific Notes

- SMIC180 IO cells use `tpd018bcdnv5` device masters.
- Consumer voltage domains (PVDD1CDG/PVSS1CDG) are typically 0.9V or 1.8V.
- Provider voltage domains (PVDD2CDG/PVSS2CDG) are typically 1.8V or 3.3V.
- Use non-round voltage values in stimulus (e.g., 0.87, 1.72) to avoid convergence issues.

---

## 7. AC Measurement Extraction

The default `measurements.json` pipeline only extracts DC and transient data.
For designs with AC analysis, Agent must **additionally** extract AC metrics
from the PSF files:

### Source file
- `spectre/deck.raw/ac.ac` 鈥?AC sweep results in PSF ASCII format

### Required metrics

| Metric | Calculation |
|--------|------------|
| DC Gain | `20*log10(|TF(f_low)|)` where `TF = VOUT / VIN_ac` |
| -3dB Bandwidth | First frequency where `|TF| < DC_Gain - 3dB` |
| Unity Gain Freq | Interpolated frequency where `|TF| = 0dB` |
| Phase Margin | `180掳 + phase(TF @ UGF)` |
| Gain Margin | `-|TF| @ phase(TF) = -180掳` |

### Output format
Write extracted metrics to `measurements.json` under `ac_metrics` key,
then evaluate against spec constraints.

---

## 8. SMIC180-Specific Notes

- SMIC180 IO cells use `tpd018bcdnv5` device masters.
- Consumer voltage domains (PVDD1CDG/PVSS1CDG) are typically 0.9V or 1.8V.
- Provider voltage domains (PVDD2CDG/PVSS2CDG) are typically 1.8V or 3.3V.
- Use non-round voltage values in stimulus (e.g., 0.87, 1.72) to avoid convergence issues.
- **Spectre mode**: Use `--spectre-mode spectre` (no `+preset` flags for Spectre 18.1).
- **AC parameter**: vsource AC magnitude is `acm` in CDF, which Spectre netlists as `mag=1`.

## 9. Spectre AC Source Syntax Reference

### vsource (voltage source)
```spectre
VIP_SRC (VIP 0) vsource dc=0.9 mag=1 phase=0 type=dc
```
- `dc` — DC operating point voltage
- `mag` — AC magnitude (for small-signal AC analysis). **NOT** `acmag` or `ac`
- `phase` — AC phase in degrees
- `type=dc` — source type (default: `dc`)
- For **DC-only** bias: use `vdc` (analogLib) instead of `vsource`

### isource (current source)
```spectre
IBIAS_SRC (IBIAS 0) isource dc=-10u mag=1e-6 phase=0
```
- `dc` — DC operating point current (negative = current flows into the node)
- `mag` — AC magnitude for small-signal analysis
- Use `isource` for **bias current** pins (e.g., IBIAS on OpAmps)
- Do NOT use `vsource` for current bias — it creates a voltage source, not a current source

### AC Analysis Configuration
```json
{
  "name": "ac",
  "enabled": true,
  "sweep": {
    "param": "freq",
    "start": "1",
    "stop": "10G",
    "dec": "100"
  }
}
```
- Always specify `dec` (decades per octave) for frequency sweeps
- `start` should be low enough to capture DC gain (typically 1Hz or 10Hz)
- `stop` should be high enough to capture unity-gain frequency (typically 1GHz for 180nm)

### Common Mistakes to Avoid
1. **DO NOT** use `acmag` or `ac` as vsource parameters — Spectre 18.1 uses `mag` and `phase`
2. **DO NOT** use `vsource` for current bias pins — use `isource` instead
3. **DO NOT** forget `phase=0` for AC sources — Spectre requires both `mag` and `phase`
4. **DO NOT** mix `vsource` with `ac` parameter — the AC info goes in `mag` and `phase` fields

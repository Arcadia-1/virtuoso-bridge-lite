# SMIC180 IO Ring Simulation Suite

SMIC 180nm (eFoundry 2P4M) simulation and general analog-circuit optimization automation. Contains one skill under `skills/`:

- `smic180-simulator` - Testbench building, Spectre simulation, and Analog Optimization V2

## PDK

```
/home/IC/Tech/smic18ee_2P6M_20100810
```

## Quick Start

1. Install dependencies (reuse from main venv):
   ```bash
   pip install pyyaml
   ```

2. Configure site settings:
   ```bash
   cp _local/site.yaml.template _local/site.yaml
   # Edit _local/site.yaml with your paths
   ```

3. Run config check:
   ```bash
   python tools/smic180_config_check.py
   ```

4. Run simulation:
   ```bash
   # Follow skills/smic180-simulator/SKILL.md Step 0-4
   ```

## Analog Optimization V2 Quick Start

V2 has a strict configuration format that is not compatible with the old
optimizer configuration or `sim_config.json`. The complete validated JSON
example and parameter/PVT guidance are in
`skills/smic180-simulator/SKILL.md#analog-optimization-v2`.

```bash
python skills/smic180-simulator/scripts/analog_optimize.py validate --config analog_opt_v2.json
python skills/smic180-simulator/scripts/analog_optimize.py evaluate --config analog_opt_v2.json --candidate candidate.json
python skills/smic180-simulator/scripts/analog_optimize.py run --config analog_opt_v2.json [--replace-work-cell] [--replace-result-cell]
python skills/smic180-simulator/scripts/analog_optimize.py resume --run-dir output/analog_optimization/run
python skills/smic180-simulator/scripts/analog_optimize.py report --run-dir output/analog_optimization/run
```

`validate` and `report` are offline. `evaluate` consumes a physical candidate
object such as `{"M1_W": 1.0e-5}`. `run` performs search, fresh best replay,
PVT validation, reporting, and conditional publication; `resume` continues from
the manifests and workflow state in the original run directory.

Stimuli are fixed unless explicitly marked `optimizable: true` and paired with
a bounded `target: "bias"` parameter. MOS `W/L/fingers/m`, resistor and
capacitor CDF properties, bias sources, and Spectre design variables are
separate, explicitly bounded optimization targets. PVT corners, voltages, and
temperatures are post-search validation conditions; `voltage_stimulus` names the
supply stimulus changed by the voltage grid. The complete minimum JSON in the
simulator SKILL includes MOS width, fixed `VDD` with `source_instance`, DC-op and
AC, a gain constraint, seeded random search, and TT/3.3 V/27 C PVT.

Ordinary AC analysis does not provide `phase_margin`; loop stability requires a
dedicated STB setup. Source, work, and result cells must be distinct. Existing
work/result cells are protected unless the corresponding replace flag is
explicitly supplied. Runs retain the resolved config, manifests, search history,
candidate artifacts, best replay, PVT results, report, and DC-sweep SVGs so an
interrupted run can be resumed without reconstructing its state by hand.

## Documentation

- Simulator: `skills/smic180-simulator/SKILL.md`
- Agent instructions: `AGENTS.md`

## File Structure

```text
smic180-ioring/
|-- _local/
|   |-- site.yaml              # Site configuration (fill this in)
|   `-- site.yaml.template     # Template for site configuration
|-- skills/
|   `-- smic180-simulator/     # Simulation skill
|-- tools/
|   |-- smic180_site_config/   # Site configuration loader
|   |-- smic180_config_export.py
|   |-- smic180_config_check.py
|   `-- IC_prompt_builder.html # Interactive prompt builder
|-- output/
|   `-- simulation/            # Simulation output root
|-- .env                       # Environment variables
|-- AGENTS.md                  # Agent instructions
`-- README.md                  # This file
```

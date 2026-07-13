# Agent Instructions

This repository is a SMIC 180nm IO ring simulation suite. It contains one skill under `skills/`:

- `smic180-simulator`

Treat the simulation workflow as high-risk EDA automation. Small changes can create invalid testbenches, wrong pin classifications, Spectre errors, or misleading simulation results.

## Registration

Register `skills/smic180-simulator/` as the Agent skill root.

Do not register the repository root as a single skill.

## Output Rules

Use the shared output root:

```text
${AMS_OUTPUT_ROOT}/simulation/<timestamp>/
${AMS_OUTPUT_ROOT}/simulation/.latest_run
${AMS_OUTPUT_ROOT}/drc/
${AMS_OUTPUT_ROOT}/lvs/
${AMS_OUTPUT_ROOT}/pex/
```

## Configuration

Use one local site configuration file:

```text
_local/site.yaml
```

Create it from `_local/site.yaml.template`. It is ignored by git and is the only normal Agent-facing place for site-specific values:

```text
project.output_root
bridge.fs_mode
bridge.disable_control_master
cadence.cds_lib_180
cadence.ic_root
cadence.mmsim_root
calibre.mgc_home
calibre.pdk_layermap_180
calibre.lvs_include_180
spectre.core_model_include
spectre.core_sections
spectre.io_model_include
spectre.lm_license_file
spectre.cds_lic_file
```

Keep `~/.virtuoso-bridge/.env` separate; it is owned by `virtuoso-bridge init` and stores bridge connection values.

Run `tools/smic180_config_check.py` before long simulation flows.

## PDK Path

```
/home/IC/Tech/smic18ee_2P6M_20100810
```

This is the SMIC 180nm eFoundry 2P4M PDK. All PDK references (models, layermap, cds.lib, LVS rules) point under this root.

## Simulator Guardrails

Follow `skills/smic180-simulator/SKILL.md` for symbol export, pin intent authoring, testbench build, and Spectre run order.

Do not skip the LLM authoring step for real validation:

1. Read `references/pin_classification.md`.
2. Read `<run_dir>/pin_info.json`.
3. Write `<run_dir>/pin_classifications.json`.
4. Read `references/sim_config_rules.md`.
5. Write `<run_dir>/sim_config.json`.

Treat these as high-risk simulator logic:

```text
skills/smic180-simulator/sim_io/pin_types.py
skills/smic180-simulator/sim_io/flow.py
skills/smic180-simulator/sim_io/sim/corner.py
skills/smic180-simulator/sim_io/sim/spec_check.py
skills/smic180-simulator/sim_io/sim/
skills/smic180-simulator/sim_io/maestro/result_utils.py
skills/smic180-simulator/sim_io/maestro/
skills/smic180-simulator/skill_code/
skills/smic180-simulator/references/pin_classification.md
skills/smic180-simulator/references/sim_config_rules.md
```

Before editing simulator core code, rule out:

1. stale or missing `pin_classifications.json`
2. stale or missing `sim_config.json`
3. wrong `SIM_CDS_LIB` or `CDS_LIB_PATH_180`
4. missing model include paths
5. Virtuoso bridge or Spectre license/environment issues

## Headless CIW Mode Notes

The Virtuoso CIW daemon runs headless (no GUI window). This affects schematic operations:

- **`schematic_create_wire_between_instance_terms`**: FAILS in headless mode. Use `label_term_directed()` from `bridge/edit_patterns.py` instead.
- **`schCreateWireLabel`** and **`schCreatePin`**: FAIL in headless mode (need editor window). Use net-label-based wiring instead.
- **`setof` with complex expressions**: Unreliable. Use `foreach(inst cv~>instances when(...) ...)` pattern.
- **Instance master terminals**: `inst~>master~>terminals` returns nil for headless-created instances.
- **`open_cell_view(mode="w")`**: Creates a NEW empty cellview. Use `mode="a"` to edit existing.

## Spectre AC Source Syntax

When writing `pin_classifications.json` for analog circuits:
- **vsource AC**: `stimulus_params: {"dc": "0.9", "acm": "1", "acp": "0"}` (NOT `mag`/`phase`)
- **isource for bias**: `stimulus: "isource", stimulus_params: {"dc": "-10u"}` (NOT `vsource`)
- **AC analysis**: Set `acm > 0` on ONE input of a differential pair only

The Spectre netlist uses `mag` and `phase` (instance parameters), but CDF uses `acm` and `acp`.
The TB builder and deck builder handle the translation automatically.

## Analog Circuit Support

The simulator supports analog-only circuits (OpAmp, Bandgap, LDO) via device classes:
- `analog_input` -> places `vdc` with DC bias + optional AC stimulus
- `analog_output` -> places `cap` load
- `analog_power` -> places `idc` inner source (connected to PVSS inner pin)
- `analog_ground` -> places `vdc` PVSS device (near-zero voltage)
- `bias_current` -> places `isource` (current source, NOT vsource)

See `skills/smic180-simulator/references/pin_classification.md` Section 6 for examples.

## Validation

After any code change, run the smallest relevant check:

```bash
python skills/smic180-simulator/scripts/symbol_export.py --help
python skills/smic180-simulator/scripts/tb_builder.py --help
python skills/smic180-simulator/scripts/spectre_runner.py --help
# verify --corners flag is present
python skills/smic180-simulator/scripts/optimizer.py --help
# verify --batch-size flag is present
python -c "import sim_io; import sim_io.flow; import sim_io.site_config"
```

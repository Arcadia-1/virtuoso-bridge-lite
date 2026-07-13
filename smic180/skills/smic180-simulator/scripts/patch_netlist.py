import re, json, sys
from pathlib import Path

run_dir = Path(sys.argv[1])
netlist_path = run_dir / "spectre" / "netlist.scs"
pin_class_path = run_dir / "pin_classifications.json"

if not netlist_path.exists() or not pin_class_path.exists():
    print("Missing netlist or pin_classifications")
    sys.exit(1)

netlist = netlist_path.read_text(encoding="utf-8")
pins = json.loads(pin_class_path.read_text(encoding="utf-8")).get("pins", [])

# Build a map of instance name -> params from pin classifications
patch_map = {}
for p in pins:
    name = p.get("name", "")
    stim = p.get("stimulus_params", {})
    load_p = p.get("load_params", {})

    # Determine the instance name
    inst_name = None
    if p.get("device_class") == "analog_ground":
        inst_name = f"PVSS_{name}"
        if "dc" not in stim:
            stim = {"vdc": "0"}
    elif p.get("device_class") == "analog_output":
        inst_name = f"LOAD_{name}"
        if load_p:
            stim = load_p
    elif stim:
        inst_name = f"SRC_{name}"

    if inst_name and stim:
        patch_map[inst_name] = stim

# Patch the netlist
lines = netlist.split("\n")
patched = []
for line in lines:
    stripped = line.strip()
    patched_line = line

    for inst, params in patch_map.items():
        if stripped.startswith(f"{inst} ("):
            # Parse: INST_NAME ( nets ) device_type params
            # e.g. SRC_AVD (AVD 0) vsource type=dc
            # We need to add/replace dc=, mag=, etc.
            parts = stripped.split(")", 1)
            if len(parts) == 2:
                prefix = parts[0] + ")"
                suffix = parts[1]

                # For vsource with dc param
                if "dc" in params and "vsource" in suffix:
                    dc_val = params["dc"]
                    if "dc=" in suffix:
                        suffix = re.sub(r'dc=[^\s]+', f'dc={dc_val}', suffix)
                    else:
                        suffix = suffix.replace("vsource", f"vsource dc={dc_val}")
                    # Fix type
                    suffix = re.sub(r'type=sine', 'type=dc', suffix)

                # For vsource with mag/phase (AC)
                if "acm" in params:
                    mag_val = params["acm"]
                    if "mag=" in suffix:
                        suffix = re.sub(r'mag=[^\s]+', f'mag={mag_val}', suffix)
                    else:
                        suffix = suffix.replace("vsource", f"vsource mag={mag_val}")
                if "acp" in params:
                    phase_val = params["acp"]
                    if "phase=" in suffix:
                        suffix = re.sub(r'phase=[^\s]+', f'phase={phase_val}', suffix)
                    else:
                        suffix = suffix.replace("vsource", f"vsource phase={phase_val}")

                # For isource with dc param
                if "dc" in params and "isource" in suffix:
                    dc_val = params["dc"]
                    if "dc=" in suffix:
                        suffix = re.sub(r'dc=[^\s]+', f'dc={dc_val}', suffix)
                    else:
                        suffix = suffix.replace("isource", f"isource dc={dc_val}")
                    suffix = re.sub(r'type=sine', 'type=dc', suffix)

                # For vdc with vdc param
                if "vdc" in params and "vsource" in suffix:
                    vdc_val = params["vdc"]
                    if "dc=" in suffix:
                        suffix = re.sub(r'dc=[^\s]+', f'dc={vdc_val}', suffix)
                    else:
                        suffix = suffix.replace("vsource", f"vsource dc={vdc_val}")

                # For capacitor with c param
                if "c" in params and "capacitor" in suffix:
                    c_val = params["c"]
                    if "c=" in suffix:
                        suffix = re.sub(r'c=[^\s]+', f'c={c_val}', suffix)
                    else:
                        suffix = suffix.replace("capacitor", f"capacitor c={c_val}")

                patched_line = prefix + suffix

    patched.append(patched_line)

netlist_path.write_text("\n".join(patched), encoding="utf-8")
print(f"Patched {len(patch_map)} instances in {netlist_path}")
for inst, params in patch_map.items():
    print(f"  {inst}: {params}")

#!/usr/bin/env python3
"""SMIC180 Circuit Parameter Optimizer.

Optimizes actual circuit design parameters (transistor W/L/M) by:
  1. Backing up the original schematic to {cell}_orig on first run
  2. Each iteration: write params to schematic -> re-export netlist -> run Spectre
  3. Restoring the original schematic when optimization completes

Usage:
    python scripts/optimizer.py --run-dir <run_dir> --generate-config
    python scripts/optimizer.py --run-dir <run_dir> --config optimization/optimizer_config.json
"""
from __future__ import annotations
import argparse, copy, json, re, sys, time
from pathlib import Path

_VB_LITE_SKILLS = Path(__file__).resolve().parents[4] / "virtuoso-bridge-lite" / "skills" / "optimizer"
if _VB_LITE_SKILLS.is_dir():
    sys.path.insert(0, str(_VB_LITE_SKILLS))
from optimizer_engine import OptConfig as EngineConfig, run_optimization_loop
_SIM_IO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SIM_IO))


_INST_RE = re.compile(r'^(\S+)\s+\(([^)]+)\)\s+(\S+)\s+(.*)', re.MULTILINE)
_PARAM_RE = re.compile(r'(\w+)=\(([^)]+)\)')

def parse_netlist_instances(netlist_text):
    instances = {}
    # Flatten line continuations (backslash-newline) before parsing
    flat = re.sub(r'\\\s*\n\s*', ' ', netlist_text)
    subckt_match = re.search(r'subckt\s+(\S+)\s+.*?^ends\s+\1\b', flat, re.MULTILINE | re.DOTALL)
    if not subckt_match:
        return instances
    subckt_text = subckt_match.group(0)
    # Match each line independently (handles leading whitespace)
    for line in subckt_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("subckt") or line.startswith("ends") or line.startswith("//"):
            continue
        m = re.match(r"(\S+)\s+\(([^)]+)\)\s+(\S+)\s+(.*)", line)
        if m:
            inst_name, nets, cell, rest = m.group(1), m.group(2).split(), m.group(3), m.group(4)
            params = {pm.group(1): pm.group(2) for pm in _PARAM_RE.finditer(rest)}
            instances[inst_name] = {"cell": cell, "nets": nets, "params": params}
    return instances

def _parse_si_param(val):
    val = val.strip()
    for sfx, sc in {"T":1e12,"G":1e9,"M":1e6,"K":1e3,"k":1e3,"m":1e-3,"u":1e-6,"n":1e-9,"p":1e-12,"f":1e-15}.items():
        if val.endswith(sfx):
            try: return float(val[:-1]) * sc
            except ValueError: return None
    try: return float(val)
    except ValueError: return None

def _scale_si(val, factor):
    r = val * factor
    if r >= 1e-3: return f"{r*1e3:.1f}m" if r < 1 else f"{r:.1f}"
    elif r >= 1e-6: return f"{r*1e6:.1f}u"
    elif r >= 1e-9: return f"{r*1e9:.0f}n"
    elif r >= 1e-12: return f"{r*1e12:.0f}p"
    else: return f"{r*1e15:.0f}f"

def _parse_multiplier(val):
    nums = re.findall(r'\d+', val)
    result = 1
    for n in nums: result *= int(n)
    return float(result) if nums else None


_PARAM_MAP = {"nf": "fingers", "wf": "Wfg"}

# --- Topology-Aware Parameter Selection (uses virtuoso-bridge-lite read_schematic) ---

_TOPOLOGY_RULES = {
    "diff_pair": {
        "detect": lambda terms_list: (
            len(terms_list) == 2
            and terms_list[0].get("S") == terms_list[1].get("S")  # shared source
            and terms_list[0].get("G") != terms_list[1].get("G")  # complementary gates
        ),
        "role": "differential_pair",
        "params": ["w", "l", "m"],  # L matters for matching, m for current
        "priority": 1,
    },
    "tail_current": {
        "detect": lambda terms_list, dp_source_net=None, dp_instances=None: (
            len(terms_list) == 1
            and dp_source_net is not None
            and terms_list[0].get("D") == dp_source_net
        ),
        "role": "tail_current_source",
        "params": ["w", "l", "m"],
        "priority": 1,
    },
    "diode_connected": {
        "detect": lambda t: t.get("G") == t.get("D"),
        "role": "diode_reference",
        "params": ["w", "l"],
        "priority": 2,
    },
    "cascode": {
        "detect": lambda terms_list, bias_nets=None: False,  # set dynamically
        "role": "cascode",
        "params": ["w", "fingers"],
        "priority": 2,
    },
    "active_load": {
        "detect": lambda terms_list, load_gate=None: (
            len(terms_list) >= 2
            and all(t.get("G") == load_gate for t in terms_list if load_gate)
        ),
        "role": "active_load",
        "params": ["w", "l", "fingers"],
        "priority": 1,
    },
    "current_mirror": {
        "detect": lambda terms_list, mirror_gate=None: (
            len(terms_list) >= 2
            and len(set(t.get("G") for t in terms_list)) == 1
        ),
        "role": "current_mirror",
        "params": ["w", "l", "m"],
        "priority": 2,
    },
}


def analyze_topology(client, lib, cell):
    """Read schematic via virtuoso-bridge-lite and classify transistors.

    Returns dict mapping instance_name -> {
        "role": str, "block": str, "priority": int,
        "tunable_params": list[str], "cell": str, "terms": dict
    }
    """
    try:
        from virtuoso_bridge.virtuoso.schematic.reader import read_schematic
    except ImportError:
        print("  [topo] WARNING: virtuoso-bridge-lite not available, skipping topology analysis")
        return {}

    data = read_schematic(client, lib, cell, include_positions=False, param_filters=None)
    instances = data.get("instances", [])
    if not instances:
        print("  [topo] WARNING: No instances found in schematic")
        return {}

    # Build instance lookup
    inst_map = {}
    for inst in instances:
        name = inst["name"]
        cell_name = inst.get("cell", "")
        terms = inst.get("terms", {})
        params = inst.get("params", {})
        inst_map[name] = {"cell": cell_name, "terms": terms, "params": params}

    # Group by cell type (PMOS vs NMOS)
    pmos = {n: i for n, i in inst_map.items() if i["cell"].startswith("p")}
    nmos = {n: i for n, i in inst_map.items() if i["cell"].startswith("n")}

    result = {}

    # --- Detect diff pair (PMOS with shared S, complementary G) ---
    dp_source_net = None
    dp_instances = []
    pmos_by_source = {}
    for name, info in pmos.items():
        s_net = info["terms"].get("S", "")
        pmos_by_source.setdefault(s_net, []).append(name)

    for s_net, names in pmos_by_source.items():
        if len(names) >= 2:
            gates = [pmos[n]["terms"].get("G", "") for n in names]
            # Diff pair: gates go to input pins (VIP/VIN or similar)
            if len(set(gates)) == len(gates):  # all different gates
                # Check if these are input-connected (heuristic: gates are pin names)
                for n in names:
                    result[n] = {
                        "role": "differential_pair",
                        "block": "diff_pair",
                        "priority": 1,
                        "tunable_params": ["w", "l", "m"],
                        "cell": pmos[n]["cell"],
                        "terms": pmos[n]["terms"],
                    }
                dp_source_net = s_net
                dp_instances = names
                break

    # --- Detect tail current source (D connects to diff pair source) ---
    if dp_source_net:
        for name, info in pmos.items():
            if name in result:
                continue
            if info["terms"].get("D") == dp_source_net:
                result[name] = {
                    "role": "tail_current_source",
                    "block": "tail_current",
                    "priority": 1,
                    "tunable_params": ["w", "l", "m"],
                    "cell": info["cell"],
                    "terms": info["terms"],
                }
                break

    # --- Detect active load (PMOS with shared gate, cross-connected to diff pair drains) ---
    pmos_remaining = {n: i for n, i in pmos.items() if n not in result}
    pmos_by_gate = {}
    for name, info in pmos_remaining.items():
        g_net = info["terms"].get("G", "")
        pmos_by_gate.setdefault(g_net, []).append(name)

    for g_net, names in pmos_by_gate.items():
        if len(names) >= 2:
            # Active load: sources are AVD, drains connect to diff pair side
            all_avd = all(pmos[n]["terms"].get("S") == "AVD" for n in names)
            if all_avd:
                for n in names:
                    result[n] = {
                        "role": "active_load",
                        "block": "active_load",
                        "priority": 1,
                        "tunable_params": ["w", "l", "fingers"],
                        "cell": pmos[n]["cell"],
                        "terms": pmos[n]["terms"],
                    }

    # --- Detect diode-connected (G == D) ---
    for name, info in {**pmos, **nmos}.items():
        if name in result:
            continue
        if info["terms"].get("G") == info["terms"].get("D") and info["terms"].get("G"):
            result[name] = {
                "role": "diode_reference",
                "block": "diode_ref",
                "priority": 2,
                "tunable_params": ["w", "l"],
                "cell": info["cell"],
                "terms": info["terms"],
            }

    # --- Detect cascode (NMOS/PMOS with gate on bias net, stacked S/D) ---
    nmos_remaining = {n: i for n, i in nmos.items() if n not in result}
    # Group NMOS by gate
    nmos_by_gate = {}
    for name, info in nmos_remaining.items():
        g_net = info["terms"].get("G", "")
        nmos_by_gate.setdefault(g_net, []).append(name)

    for g_net, names in nmos_by_gate.items():
        if len(names) >= 2:
            # Check if S/D are stacked (one's D connects to another's S)
            d_nets = {nmos[n]["terms"].get("D") for n in names}
            s_nets = {nmos[n]["terms"].get("S") for n in names}
            if d_nets & s_nets:  # overlap = stacked
                for n in names:
                    result[n] = {
                        "role": "cascode",
                        "block": "cascode_nmos",
                        "priority": 2,
                        "tunable_params": ["w", "fingers"],
                        "cell": nmos[n]["cell"],
                        "terms": nmos[n]["terms"],
                    }

    # --- Detect current mirrors (shared gate, same type) ---
    for g_net, names in nmos_by_gate.items():
        if any(n in result for n in names):
            continue  # already classified
        if len(names) >= 2:
            for n in names:
                result[n] = {
                    "role": "current_mirror",
                    "block": "mirror_nmos",
                    "priority": 2,
                    "tunable_params": ["w", "l", "m"],
                    "cell": nmos[n]["cell"],
                    "terms": nmos[n]["terms"],
                }

    pmos_remaining2 = {n: i for n, i in pmos.items() if n not in result}
    pmos_by_gate2 = {}
    for name, info in pmos_remaining2.items():
        g_net = info["terms"].get("G", "")
        pmos_by_gate2.setdefault(g_net, []).append(name)
    for g_net, names in pmos_by_gate2.items():
        if len(names) >= 2:
            for n in names:
                result[n] = {
                    "role": "current_mirror",
                    "block": "mirror_pmos",
                    "priority": 2,
                    "tunable_params": ["w", "l", "m"],
                    "cell": pmos[n]["cell"],
                    "terms": pmos[n]["terms"],
                }

    # --- Classify remaining single instances ---
    for name, info in inst_map.items():
        if name not in result:
            result[name] = {
                "role": "bias_transistor",
                "block": "bias",
                "priority": 3,
                "tunable_params": ["w"],
                "cell": info["cell"],
                "terms": info["terms"],
            }

    return result


def generate_topology_config(client, lib, cell, run_dir, spec_weights=None):
    """Generate optimizer config using topology-aware parameter selection.

    Calls analyze_topology() to identify functional blocks, then selects
    which CDF params (w, l, fingers, m) to tune for each instance based
    on its role in the circuit.
    """
    topo = analyze_topology(client, lib, cell)
    if not topo:
        print("  [topo] No topology info; falling back to default config")
        return generate_default_config(run_dir)

    cp = run_dir / "pin_classifications.json"
    pin_data = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {"pins": []}
    netlist_path = run_dir / "spectre" / "netlist.scs"
    instances = {}
    if netlist_path.exists():
        instances = parse_netlist_instances(netlist_path.read_text(encoding="utf-8"))

    # Group instances by block for parameter coupling
    blocks = {}
    for inst_name, info in topo.items():
        block = info["block"]
        blocks.setdefault(block, []).append(inst_name)

    params = []
    seen = set()

    # Priority 1: diff pair, tail current, active load
    for priority_level in [1, 2, 3]:
        for inst_name, info in sorted(topo.items()):
            if info["priority"] != priority_level:
                continue
            pp = instances.get(inst_name, {}).get("params", {})
            if not pp:
                pp = info.get("params", {})

            for cdf_param in info["tunable_params"]:
                param_key = f"{inst_name}.{cdf_param}"
                if param_key in seen:
                    continue
                seen.add(param_key)

                if cdf_param == "w" and "w" in pp:
                    wv = _parse_si_param(pp["w"])
                    if wv and wv > 0:
                        params.append({
                            "name": param_key,
                            "target": "circuit",
                            "instance": inst_name,
                            "cdf_param": "w",
                            "dtype": "str",
                            "low": _scale_si(wv, 0.5),
                            "high": _scale_si(wv, 2.0),
                            "original": pp["w"],
                            "role": info["role"],
                            "block": info["block"],
                        })

                elif cdf_param == "l" and "l" in pp:
                    lv = _parse_si_param(pp["l"])
                    if lv and lv > 0:
                        params.append({
                            "name": param_key,
                            "target": "circuit",
                            "instance": inst_name,
                            "cdf_param": "l",
                            "dtype": "str",
                            "low": _scale_si(lv, 0.75),
                            "high": _scale_si(lv, 1.5),
                            "original": pp["l"],
                            "role": info["role"],
                            "block": info["block"],
                        })

                elif cdf_param == "fingers" and "nf" in pp:
                    fv = _parse_multiplier(pp["nf"])
                    if fv and fv > 0:
                        params.append({
                            "name": param_key,
                            "target": "circuit",
                            "instance": inst_name,
                            "cdf_param": "fingers",
                            "dtype": "int",
                            "low": max(1, int(fv * 0.5)),
                            "high": int(fv * 2) + 1,
                            "original": pp["nf"],
                            "role": info["role"],
                            "block": info["block"],
                        })

                elif cdf_param == "m" and "m" in pp:
                    mv = _parse_multiplier(pp["m"])
                    if mv and mv > 0:
                        params.append({
                            "name": param_key,
                            "target": "circuit",
                            "instance": inst_name,
                            "cdf_param": "m",
                            "dtype": "int",
                            "low": max(1, int(mv * 0.5)),
                            "high": int(mv * 2) + 1,
                            "original": pp["m"],
                            "role": info["role"],
                            "block": info["block"],
                        })

    # Add IBIAS stimulus param
    for pin in pin_data.get("pins", []):
        n, stim, sp = pin.get("name", ""), pin.get("stimulus"), pin.get("stimulus_params") or {}
        if stim == "idc" and "dc" in sp:
            raw = str(sp["dc"]).rstrip("mAauA")
            try:
                v = float(raw)
                unit = "m" if sp["dc"].endswith("m") else ""
                params.append({
                    "name": f"{n}.dc",
                    "target": "pin_classifications",
                    "path": f"pins[{n}].stimulus_params.dc",
                    "low": round(v * 0.5, 4),
                    "high": round(v * 2.0, 4),
                    "dtype": "float",
                    "unit": unit,
                    "role": "bias_current",
                    "block": "stimulus",
                })
            except ValueError:
                pass

    cc = sum(1 for p in params if p.get("target") == "circuit")
    sc = sum(1 for p in params if p.get("target") == "pin_classifications")

    # Print topology summary
    print(f"\n[topo] Topology Analysis Summary:")
    blocks_summary = {}
    for inst_name, info in topo.items():
        role = info["role"]
        blocks_summary.setdefault(role, []).append(inst_name)
    for role, insts in blocks_summary.items():
        print(f"  {role}: {', '.join(insts)}")
    print(f"\n[topo] Generated {len(params)} params: {cc} circuit + {sc} stimulus")

    if spec_weights is None:
        spec_weights = {}

    return {
        "params": params,
        "max_iterations": 50,
        "n_init": 5,
        "seed": 42,
        "spec_weights": spec_weights,
        "topology_summary": {inst: {"role": info["role"], "block": info["block"]}
                             for inst, info in topo.items()},
    }



def set_instance_params_headless(client, lib, cell, inst_name, params):
    """Set CDF parameters on a schematic instance.

    Tries setInstParams first (preferred), falls back to dbReplaceProp.
    When setting 'w' via dbReplaceProp, also sets 'fw' (finger width)
    because the si netlister uses simW=iPar("fw") not w directly.
    """
    if not params: return
    mapped = {_PARAM_MAP.get(k, k): v for k, v in params.items()}
    # Try setInstParams first
    pairs = " ".join(f'"{k}" "{v}"' for k, v in mapped.items())
    skill = f'setInstParams("{lib}" "{cell}" "{inst_name}" list({pairs}))'
    r = client.execute_skill(skill, timeout=30)
    if not r.errors:
        print(f"  [params] {inst_name}: {', '.join(f'{k}={v}' for k, v in mapped.items())}")
        return
    # Fallback: dbReplaceProp(inst "name" "string" "value")
    # Build list of (prop_name, value) pairs to set
    props_to_set = []
    for k, v in mapped.items():
        props_to_set.append((k, v))
        # When setting w, also set fw (finger width) so si netlister picks it up
        if k == "w" and ("fw", v) not in props_to_set:
            props_to_set.append(("fw", v))
    # Set all props in a single SKILL call (one cv open/close)
    props_skill = " ".join(
        f'dbReplaceProp(inst "{k}" "string" "{v}")' for k, v in props_to_set
    )
    skill2 = (
        f'let((cv inst) '
        f'cv = dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a") '
        f'inst = car(setof(x cv~>instances x~>name == "{inst_name}")) '
        f'if(inst then {props_skill} schCheck(cv) dbSave(cv) dbClose(cv) t))'
    )
    r2 = client.execute_skill(skill2, timeout=15)
    if r2.errors:
        print(f"  [params] WARNING: {inst_name} failed: {r2.errors[:1]}")
    print(f"  [params] {inst_name} (fallback): {', '.join(f'{k}={v}' for k, v in mapped.items())}")


def _sch_check_and_save(client, lib, cell):
    """Open schematic, run schCheck + dbSave, all in one SKILL call."""
    skill = (
        f'let((cv) cv = dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a") '
        f'if(cv then schCheck(cv) dbSave(cv) t))'
    )
    r = client.execute_skill(skill, timeout=15)
    if r.errors:
        print(f"  [sch] WARNING: schCheck+save failed for {lib}/{cell}: {r.errors[:2]}")



def _read_inst_params(client, lib, cell, inst_name):
    """Read CDF params (w, l, fingers, m) for a single instance via SKILL."""
    sk = (
        'let((cv inst cdf) '
        'cv = dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "r") '
        'inst = car(setof(x cv~>instances x~>name == "{inst}")) '
        'cdf = cdfGetInstCDF(inst) '
        'sprintf(nil "%s|%s|%s|%s" '
        '  get(cdf "w")~>value get(cdf "l")~>value '
        '  get(cdf "fingers")~>value get(cdf "m")~>value))'
    ).format(lib=lib, cell=cell, inst=inst_name)
    r = client.execute_skill(sk, timeout=10)
    if r.status.value == "success" and r.output:
        parts = r.output.strip().strip('"').split("|")
        if len(parts) == 4:
            return {"w": parts[0], "l": parts[1], "fingers": parts[2], "m": parts[3]}
    return None


def backup_schematic(client, lib, cell, run_dir=None):
    """Backup original schematic params from the netlist to JSON.

    Reads instance params directly from the netlist (which reflects the
    original schematic before any optimizer modifications). This is
    reliable even if a previous run left the CDF in a modified state.
    Also creates ``{cell}_orig`` cellview for visual reference.
    """
    import json as _json
    params_backup = {}
    netlist_path = run_dir / "spectre" / "netlist.scs" if run_dir else None
    if netlist_path and netlist_path.exists():
        instances = parse_netlist_instances(netlist_path.read_text(encoding="utf-8"))
        for inst_name, info in instances.items():
            pp = info.get("params", {})
            # Netlist has w, l, m as SI strings; fingers is "nf" in netlist
            entry = {}
            if "w" in pp: entry["w"] = pp["w"]
            if "l" in pp: entry["l"] = pp["l"]
            if "nf" in pp: entry["fingers"] = pp["nf"]
            if "m" in pp: entry["m"] = pp["m"]
            if entry:
                params_backup[inst_name] = entry
    if run_dir and params_backup:
        bp_path = run_dir / "optimization" / "_original_params.json"
        bp_path.parent.mkdir(parents=True, exist_ok=True)
        bp_path.write_text(_json.dumps(params_backup, indent=2), encoding="utf-8")
        print(f"  [backup] Saved {len(params_backup)} instance params from netlist")
    # Also create _orig cellview for visual reference
    orig = f"{cell}_orig"
    r = client.execute_skill(
        f'dbOpenCellViewByType("{lib}" "{orig}" "schematic" "schematic" "r")', timeout=10)
    if r.output and "nil" not in (r.output or "").lower():
        print(f"  [backup] {lib}/{orig} exists, skipping cellview copy")
    else:
        sk = (
            f'let((cv origCv) '
            f'cv = dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a") '
            f'if(cv then '
            f'  schCheck(cv) dbSave(cv) '
            f'  origCv = dbCopyCellView(cv "{lib}" "{orig}" "schematic" "" nil) '
            f'  if(origCv then dbSave(origCv) t else nil) '
            f'else nil))'
        )
        r = client.execute_skill(sk, timeout=30)
        if r.errors:
            print(f"  [backup] dbCopyCellView: {r.errors[:2]}")
        else:
            print(f"  [backup] Cellview backed up to {lib}/{orig}")
    return True


def restore_schematic(client, lib, cell, run_dir=None):
    """Restore schematic by re-applying original params.

    Uses the _original_params.json saved during backup.
    """
    import json as _json
    bp_path = run_dir / "optimization" / "_original_params.json" if run_dir else None
    if bp_path and bp_path.exists():
        original = _json.loads(bp_path.read_text(encoding="utf-8"))
        for inst_name, params in original.items():
            set_instance_params_headless(client, lib, cell, inst_name, params)
        _sch_check_and_save(client, lib, cell)
        print(f"  [restore] Restored {len(original)} instances from _original_params.json")
        return True
    # Fallback: try dbCopyCellView
    orig = f"{cell}_orig"
    sk = (
        f'let((origCv) '
        f'origCv = dbOpenCellViewByType("{lib}" "{orig}" "schematic" "schematic" "r") '
        f'if(origCv then '
        f'  dbCopyCellView(origCv "{lib}" "{cell}" "schematic" "" nil) '
        f'  t '
        f'else nil))'
    )
    r = client.execute_skill(sk, timeout=30)
    if r.errors:
        print(f"  [restore] WARNING: {r.errors[:2]}")
        return False
    print(f"  [restore] Restored from {lib}/{orig} (dbCopyCellView)")
    return True


def save_optimized_schematic(client, lib, cell, run_dir=None):
    """Save the current (optimized) schematic params to JSON + {cell}_opt cellview.

    Called AFTER best params have been applied but BEFORE restore.
    """
    import json as _json
    opt_cell = f"{cell}_opt"
    # Save current params as JSON
    if run_dir:
        opt_params = {}
        netlist_path = run_dir / "spectre" / "netlist.scs"
        if netlist_path.exists():
            instances = parse_netlist_instances(netlist_path.read_text(encoding="utf-8"))
            for inst_name in instances:
                p = _read_inst_params(client, lib, cell, inst_name)
                if p:
                    opt_params[inst_name] = p
        opt_path = run_dir / "optimization" / "_optimized_params.json"
        opt_path.parent.mkdir(parents=True, exist_ok=True)
        opt_path.write_text(_json.dumps(opt_params, indent=2), encoding="utf-8")
        print(f"  [opt] Saved {len(opt_params)} optimized params to {opt_path.name}")
    # Attempt dbCopyCellView for visual reference
    sk = (
        f'let((cv optCv) '
        f'cv = dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a") '
        f'if(cv then '
        f'  optCv = dbCopyCellView(cv "{lib}" "{opt_cell}" "schematic" "" nil) '
        f'  if(optCv then dbSave(optCv) t else nil) '
        f'else nil))'
    )
    r = client.execute_skill(sk, timeout=30)
    if r.errors:
        print(f"  [opt] dbCopyCellView: {r.errors[:2]} (params saved to JSON)")
    else:
        print(f"  [opt] Optimized schematic saved as {lib}/{opt_cell}")
    return True

def _parse_json_path(path):
    if path.startswith("pins["):
        end = path.index("]")
        return path[5:end], path[end+1:].lstrip(".")
    return "", path

def _set_nested(obj, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        if isinstance(obj, dict): obj = obj.setdefault(part, {})
    if isinstance(obj, dict): obj[parts[-1]] = value

def _write_param(data, name, path, target, value, dtype="float", unit=""):
    if dtype == "int": value = int(round(value))
    else: value = round(value, 6)
    if target == "pin_classifications":
        pin_name, sub = _parse_json_path(path)
        if pin_name:
            for p in data.get("pins", []):
                if p.get("name") == pin_name:
                    formatted = f"{value}{unit}" if unit else (str(value) if dtype == "float" else value)
                    _set_nested(p, sub, formatted)
                    return
    else:
        _set_nested(data, path, value)


def _postprocess_measurements(raw_measurements, pin_classif_data, vdd_value=1.8, eval_dir=None):
    """Convert raw PSF measurements to optimizer-compatible bridge format.

    Fixes two issues in the raw measurement pipeline:
      1. Current/power data for supply pins: PSF stores current signals as ac:SRC_*:p
         but parse_results doesn't try the ac: prefix when looking up current.
      2. DC voltage for output pins: DC operating point data isn't in PSF format;
         we extract it from pin_classifications stimulus params or PSF data.
    """
    import math
    if "_error" in raw_measurements:
        return raw_measurements

    pins = raw_measurements.get("pins", {})
    if not pins:
        return raw_measurements

    # Build pin classification lookup
    pin_map = {}
    if pin_classif_data and "pins" in pin_classif_data:
        for p in pin_classif_data["pins"]:
            pin_map[p["name"]] = p

    # Try to load PSF data for current extraction
    ac_data = {}
    if eval_dir:
        try:
            from sim_io.sim.viz import parse_psf_ascii
            for psf_file in (eval_dir / "spectre").rglob("*.ac"):
                parsed = parse_psf_ascii(psf_file)
                if hasattr(parsed, "signals"):
                    ac_data = parsed.signals
                break
        except Exception:
            pass


    # Try to load noise PSF data
    noise_data = {}
    if eval_dir:
        try:
            from sim_io.sim.viz import parse_psf_ascii
            for psf_file in (eval_dir / "spectre").rglob("*.noise"):
                parsed = parse_psf_ascii(psf_file)
                if hasattr(parsed, "signals"):
                    noise_data = parsed.signals
                break
        except Exception:
            pass

    # Try to extract PSRR from AC data (VOUT/VDD gain)
    psrr_db = None
    if ac_data:
        import math as _math
        ac_vout = ac_data.get("ac:VOUT", [])
        ac_avd = ac_data.get("ac:AVD", [])
        if ac_vout and ac_avd:
            # PSRR = |VOUT/AVD| at each freq, take DC value
            r0_v, i0_v = ac_vout[0] if isinstance(ac_vout[0], (list, tuple)) else (ac_vout[0], 0)
            r0_a, i0_a = ac_avd[0] if isinstance(ac_avd[0], (list, tuple)) else (ac_avd[0], 0)
            mag_v = _math.sqrt(r0_v**2 + i0_v**2)
            mag_a = _math.sqrt(r0_a**2 + i0_a**2)
            if mag_a > 1e-20:
                psrr_db = 20 * _math.log10(mag_v / mag_a) if mag_v > 1e-20 else -200

    # Try to extract CMRR from AC data (VOUT/VIN common-mode gain)
    # CMRR = differential_gain / common_mode_gain
    # When both inputs have AC stimulus (common-mode injection), CMRR = |VOUT_AC / VIN_AC|
    cmrr_db = None
    if ac_data:
        ac_vout_cm = ac_data.get("ac:VOUT", [])
        ac_vip_cm = ac_data.get("ac:VIP", [])
        ac_vin_cm = ac_data.get("ac:VIN", [])
        # Use VIP if available, otherwise VIN
        ac_input_cm = ac_vip_cm if ac_vip_cm else ac_vin_cm
        if ac_vout_cm and ac_input_cm:
            r0_vout, i0_vout = ac_vout_cm[0] if isinstance(ac_vout_cm[0], (list, tuple)) else (ac_vout_cm[0], 0)
            r0_vin, i0_vin = ac_input_cm[0] if isinstance(ac_input_cm[0], (list, tuple)) else (ac_input_cm[0], 0)
            mag_vout = _math.sqrt(r0_vout**2 + i0_vout**2)
            mag_vin = _math.sqrt(r0_vin**2 + i0_vin**2)
            if mag_vin > 1e-20 and mag_vout > 1e-20:
                # CMRR in dB: higher is better
                # In common-mode test: CMRR = Ad/Acm, Acm = VOUT/VIN
                # For a single-ended measurement: CMRR = 20*log10(VIN/VOUT) if VOUT < VIN
                cmrr_db = abs(20 * _math.log10(mag_vin / mag_vout))


    result_pins = {}

    for pin_name, pm in pins.items():
        if "error" in pm:
            result_pins[pin_name] = pm
            continue
        new_pm = dict(pm)
        pinfo = pin_map.get(pin_name, {})
        ptype = pm.get("pad_type", pinfo.get("device_class", ""))
        stim = pinfo.get("stimulus_params", {})

        # Extract current/power for supply and bias pins
        if ptype in ("analog_power", "bias_current"):
            new_pm["voltage"] = float(stim.get("dc", vdd_value))
            # Try to get current from raw measurement first
            iavg = pm.get("iavg") or pm.get("current_avg")
            # If not found, try PSF ac data (current stored as ac:SRC_<pin>:p)
            if iavg is None and ac_data:
                src_name = f"SRC_{pin_name}"
                for cand in [f"{src_name}:p", f"{src_name}:PLUS", src_name]:
                    if cand in ac_data:
                        vals = ac_data[cand]
                        # AC data at freq=0 (index 0) gives DC operating point
                        if vals and isinstance(vals[0], (list, tuple)):
                            iavg = abs(float(vals[0][0]))  # real part at lowest freq
                        elif vals:
                            iavg = abs(float(vals[0]))
                        break
            if iavg is not None:
                new_pm["current_avg"] = float(iavg)
                new_pm["power"] = float(iavg) * new_pm["voltage"]
                # Remove stale error if we found current
                new_pm.pop("current_error", None)
            else:
                new_pm["current_error"] = pm.get("current_error", "current not measured")

        elif ptype in ("analog_ground",):
            new_pm["voltage"] = float(stim.get("dc", 0.0))

        elif ptype in ("analog_output",):
            # PSRR: if we computed it from AC data, add it
            if psrr_db is not None and "psrr" not in new_pm:
                new_pm["psrr"] = psrr_db
            if cmrr_db is not None and "cmrr" not in new_pm:
                new_pm["cmrr"] = cmrr_db
            # Noise: extract from noise PSF data
            if noise_data and "noise" not in new_pm:
                import math as _m
                # noise_data typically has "total_output_noise" or similar
                for nk, nv in noise_data.items():
                    if "total" in nk.lower() and "noise" in nk.lower():
                        if isinstance(nv, list) and len(nv) > 0:
                            val = nv[-1] if isinstance(nv[-1], (int, float)) else float(nv[-1][0]) if isinstance(nv[-1], (list, tuple)) else None
                            if val is not None:
                                new_pm["noise"] = val
                        break
                # noise density at a reference freq (1kHz)
                for nk, nv in noise_data.items():
                    if "density" in nk.lower() or "nkv" in nk.lower():
                        if isinstance(nv, list) and len(nv) > 0:
                            val = nv[0] if isinstance(nv[0], (int, float)) else float(nv[0][0]) if isinstance(nv[0], (list, tuple)) else None
                            if val is not None:
                                new_pm["noise_density"] = abs(val)
                        break
            # For output pins, vdc comes from DC analysis
            # Try to extract from PSF DC data if available
            if "vdc" not in new_pm:
                pass  # DC OP not in PSF 闂?will need separate .dc sweep
            if "vdc_nominal" not in new_pm:
                new_pm["vdc_nominal"] = 1.22  # SMIC180 bandgap nominal

        elif ptype in ("analog_input",):
            new_pm["voltage"] = float(stim.get("dc", vdd_value / 2))

        result_pins[pin_name] = new_pm

    raw_measurements["pins"] = result_pins
    return raw_measurements


def evaluate_smic180(run_dir, pin_classif_data, specs, *, dry_run=False, timeout=300):
    cp = run_dir / "pin_classifications.json"
    cp.write_text(json.dumps(pin_classif_data, indent=2, ensure_ascii=False), encoding="utf-8")
    if dry_run: return {"pins": {}, "_dry_run": True}
    try:
        from virtuoso_bridge import VirtuosoClient
        from sim_io.flow import load_dut_context
        from sim_io.sim.config import resolve_sim_config, sim_config_from_site
        from sim_io.sim.run import run_sim_run
        from sim_io.site_config import SiteConfig
        client = VirtuosoClient.from_env()
        site = SiteConfig.from_env()
        dut = load_dut_context(run_dir)
        lib, tb = dut.lib, dut.tb_cell
        circuit_cell = dut.primary_cell  # transistors live here, not in tb
        circuit_params = {}
        for s in specs:
            if s.get("target") == "circuit":
                inst, cfp, val = s.get("instance",""), s.get("cdf_param",""), s.get("_current_value","")
                if inst and cfp and val:
                    circuit_params.setdefault(inst, {})[cfp] = val
        if circuit_params:
            print(f"  [eval] Applying {sum(len(v) for v in circuit_params.values())} circuit params to {circuit_cell}...")
            for inst_name, params in circuit_params.items():
                set_instance_params_headless(client, lib, circuit_cell, inst_name, params)
            _sch_check_and_save(client, lib, circuit_cell)
        print(f"  [eval] Running Spectre for {lib}/{tb}...")
        ed = run_dir / "optimization" / f"eval_{int(time.time())}"
        ed.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(cp, ed / "pin_classifications.json")
        deck = resolve_sim_config(run_dir=run_dir, lib=lib, cell=tb, vdd_value=dut.vdd_value)
        if not deck.model_includes:
            deck.model_includes = sim_config_from_site(vdd_value=dut.vdd_value).model_includes
        sim = run_sim_run(lib, tb, dut.pins, ed, deck_config=deck, site=site, client=client,
                          spectre_timeout=timeout, vdd_value=dut.vdd_value)
        if not sim.spectre_ok: return {"_error": "Spectre failed"}
        mp = ed / "measurements.json"
        mp_data = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {"_error": "No measurements.json"}
        return _postprocess_measurements(mp_data, pin_classif_data, vdd_value=dut.vdd_value, eval_dir=ed)
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"_error": str(e)}


def compute_spec_violation(measurements, spec_weights):
    """Compute total normalized spec violation across all pins.

    Supported spec keys (per pin):
      Digital / basic:
        i_max        : current must be < value (A)
        p_max        : power must be < value (W)
        vmax_above   : peak voltage must be > value (supports *VDD)
        vmin_below   : min voltage must be < value (supports *VDD)
      Analog IC:
        gain_min     : voltage gain must be > value (V/V, linear)
        gain_db_min  : voltage gain must be > value (dB)
        bandwidth_min: -3dB bandwidth must be > value (Hz)
        psrr_max     : PSRR must be < value (dB, positive = good rejection)
        cmrr_max     : CMRR must be < value (dB)
        noise_max    : integrated noise must be < value (Vrms)
        noise_density_max: noise density must be < value (V/rtHz)
        slew_rate_min: slew rate must be > value (V/s)
        phase_margin_min: phase margin must be > value (deg)
        gain_margin_min : gain margin must be > value (dB)
        thd_max      : total harmonic distortion must be < value (ratio)
        sfdr_min     : SFDR must be > value (dB)
        vdc_accuracy : DC output must be within 闂佸憡顨忓Σ顡祃erance of target
        vdc_ripple   : peak-to-peak ripple must be < value (V)
        overshoot_max: overshoot must be < value (V)
        settling_time_max: settling time must be < value (s)
      Custom:
        custom       : user-defined expression evaluated against pm dict
    """
    total = 0.0
    vdd = measurements.get("vdd_value", 1.8)

    def _eval_bound(expr, vdd_val):
        return eval(str(expr).replace("*VDD", f"*{vdd_val}").replace("*vdd", f"*{vdd_val}"),
                     {"__builtins__": {}, "VDD": vdd_val, "vdd": vdd_val})

    for pin, pm in measurements.get("pins", {}).items():
        if "error" in pm:
            continue
        spec = spec_weights.get(pin, {})

        # 闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴?Digital / basic specs 闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴滈箖姊洪崘鎻掑辅闁稿鎹囬弻宥夊礂婢跺﹣澹曢梻浣稿暱閸樻粓宕戦幘缁樼厓闁稿繐顦禍楣冩⒑閸愭彃甯ㄩ柛瀣崌閺屽秹宕楁径濠佸闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴滈箖姊洪崘鎻掑辅闁稿鎹囬弻宥夊礂婢跺﹣澹曢梻浣稿暱閸樻粓宕戦幘缁樼厓闁稿繐顦禍楣冩⒑閸愭彃甯ㄩ柛瀣崌閺屽秹宕楁径濠佸闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴滈箖姊洪崘鎻掑辅闁稿鎹囬弻宥夊礂婢跺﹣澹曢梻浣稿暱閸樻粓宕戦幘缁樼厓闁稿繐顦禍楣冩⒑閸愭彃甯ㄩ柛瀣崌閺屽秹宕楁径濠佸闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴?
        if "i_max" in spec and ("current" in pm or "current_avg" in pm):
            a = abs(pm.get("current_avg", pm.get("current", 0)))
            l = float(spec["i_max"])
            if a > l:
                total += ((a - l) / max(l, 1e-15)) ** 2

        if "p_max" in spec and ("power" in pm or "power_avg" in pm):
            a = abs(pm.get("power_avg", pm.get("power", 0)))
            l = float(spec["p_max"])
            if a > l:
                total += ((a - l) / max(l, 1e-15)) ** 2

        if "vmax_above" in spec and "vmax" in pm:
            a = pm.get("vmax", 0)
            l = _eval_bound(spec["vmax_above"], vdd)
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        if "vmin_below" in spec and "vmin" in pm:
            a = pm.get("vmin", 0)
            l = _eval_bound(spec["vmin_below"], vdd)
            if a > l:
                total += ((a - l) / max(abs(l), 1e-15)) ** 2

        # 闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴?Analog IC specs 闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴滈箖姊洪崘鎻掑辅闁稿鎹囬弻宥夊礂婢跺﹣澹曢梻浣稿暱閸樻粓宕戦幘缁樼厓闁稿繐顦禍楣冩⒑閸愭彃甯ㄩ柛瀣崌閺屽秹宕楁径濠佸闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴滈箖姊洪崘鎻掑辅闁稿鎹囬弻宥夊礂婢跺﹣澹曢梻浣稿暱閸樻粓宕戦幘缁樼厓闁稿繐顦禍楣冩⒑閸愭彃甯ㄩ柛瀣崌閺屽秹宕楁径濠佸闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴滈箖姊洪崘鎻掑辅闁稿鎹囬弻宥夊礂婢跺﹣澹曢梻浣稿暱閸樻粓宕戦幘缁樼厓闁稿繐顦禍楣冩⒑閸愭彃甯ㄩ柛瀣崌閺屽秹宕楁径濠佸闂備礁鍟块崢婊堝磻閹剧粯鐓冮柛蹇擃槸娴滈箖姊洪崘鎻掑辅闁稿鎹囬弻宥夊礂婢跺﹣澹曢梻浣稿暱閸樻粓宕戦幘缁樼厓闁稿繐顦禍楣冩⒑閸愭彃甯ㄩ柛瀣崌閺屽秹宕楁径濠佸

        # Gain: linear (V/V) 闂?value must be > gain_min
        if "gain_min" in spec and "gain" in pm:
            a, l = float(pm["gain"]), float(spec["gain_min"])
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        # Gain: dB 闂?value must be > gain_db_min
        if "gain_db_min" in spec and "gain_db" in pm:
            a, l = float(pm["gain_db"]), float(spec["gain_db_min"])
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        # Bandwidth: Hz 闂?value must be > bandwidth_min
        if "bandwidth_min" in spec and "bandwidth" in pm:
            a, l = float(pm["bandwidth"]), float(spec["bandwidth_min"])
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        # PSRR: dB (positive = good rejection) 闂?must be > psrr_min
        if "psrr_min" in spec and "psrr" in pm:
            a, l = float(pm["psrr"]), float(spec["psrr_min"])
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        # CMRR: dB (positive = good rejection) 闂?must be > cmrr_min
        if "cmrr_min" in spec and "cmrr" in pm:
            a, l = float(pm["cmrr"]), float(spec["cmrr_min"])
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        # Noise: Vrms 闂?must be < noise_max
        if "noise_max" in spec and "noise" in pm:
            a, l = abs(float(pm["noise"])), float(spec["noise_max"])
            if a > l:
                total += ((a - l) / max(l, 1e-15)) ** 2

        # Noise density: V/rtHz 闂?must be < noise_density_max
        if "noise_density_max" in spec and "noise_density" in pm:
            a, l = abs(float(pm["noise_density"])), float(spec["noise_density_max"])
            if a > l:
                total += ((a - l) / max(l, 1e-15)) ** 2

        # Slew rate: V/s 闂?must be > slew_rate_min
        if "slew_rate_min" in spec and "slew_rate" in pm:
            a, l = abs(float(pm["slew_rate"])), float(spec["slew_rate_min"])
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        # Phase margin: deg 闂?must be > phase_margin_min
        if "phase_margin_min" in spec and "phase_margin" in pm:
            a, l = float(pm["phase_margin"]), float(spec["phase_margin_min"])
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        # Gain margin: dB 闂?must be > gain_margin_min
        if "gain_margin_min" in spec and "gain_margin" in pm:
            a, l = float(pm["gain_margin"]), float(spec["gain_margin_min"])
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        # THD: ratio 闂?must be < thd_max
        if "thd_max" in spec and "thd" in pm:
            a, l = abs(float(pm["thd"])), float(spec["thd_max"])
            if a > l:
                total += ((a - l) / max(l, 1e-15)) ** 2

        # SFDR: dB 闂?must be > sfdr_min
        if "sfdr_min" in spec and "sfdr" in pm:
            a, l = float(pm["sfdr"]), float(spec["sfdr_min"])
            if a < l:
                total += ((l - a) / max(abs(l), 1e-15)) ** 2

        # DC accuracy: vout must be within 闂佸憡顨忓Σ顡祃erance of vdc target
        if "vdc_accuracy" in spec and "vdc" in pm:
            a = float(pm["vdc"])
            tol = float(spec["vdc_accuracy"])
            nominal = float(pm.get("vdc_nominal", a))
            if abs(a - nominal) > tol:
                total += ((abs(a - nominal) - tol) / max(tol, 1e-15)) ** 2

        # DC ripple: peak-to-peak ripple must be < vdc_ripple
        if "vdc_ripple" in spec and "vdc_ripple_pp" in pm:
            a, l = float(pm["vdc_ripple_pp"]), float(spec["vdc_ripple"])
            if a > l:
                total += ((a - l) / max(l, 1e-15)) ** 2

        # Overshoot: must be < overshoot_max
        if "overshoot_max" in spec and "overshoot" in pm:
            a, l = abs(float(pm["overshoot"])), float(spec["overshoot_max"])
            if a > l:
                total += ((a - l) / max(l, 1e-15)) ** 2

        # Settling time: must be < settling_time_max
        if "settling_time_max" in spec and "settling_time" in pm:
            a, l = float(pm["settling_time"]), float(spec["settling_time_max"])
            if a > l:
                total += ((a - l) / max(l, 1e-15)) ** 2

    return total



def generate_default_config(run_dir):
    cp = run_dir / "pin_classifications.json"
    pin_data = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {"pins": []}
    netlist_path = run_dir / "spectre" / "netlist.scs"
    if not netlist_path.exists():
        print(f"WARNING: {netlist_path} not found; run spectre_runner.py first")
        return {"params": [], "max_iterations": 30, "n_init": 0, "seed": 42, "spec_weights": {}}
    instances = parse_netlist_instances(netlist_path.read_text(encoding="utf-8"))
    params = []
    for inst_name, info in instances.items():
        pp = info["params"]
        if "w" in pp:
            wv = _parse_si_param(pp["w"])
            if wv and wv > 0:
                params.append({"name":f"{inst_name}.w","target":"circuit","instance":inst_name,
                    "cdf_param":"w","dtype":"str","low":_scale_si(wv,0.3),"high":_scale_si(wv,3.0),"original":pp["w"]})
        if "l" in pp:
            lv = _parse_si_param(pp["l"])
            if lv and lv > 0:
                params.append({"name":f"{inst_name}.l","target":"circuit","instance":inst_name,
                    "cdf_param":"l","dtype":"str","low":_scale_si(lv,0.5),"high":_scale_si(lv,2.0),"original":pp["l"]})
        if "m" in pp:
            mv = _parse_multiplier(pp["m"])
            if mv and mv > 0:
                params.append({"name":f"{inst_name}.m","target":"circuit","instance":inst_name,
                    "cdf_param":"m","dtype":"int","low":max(1,int(mv*0.25)),"high":int(mv*4)+1,"original":pp["m"]})
    for pin in pin_data.get("pins", []):
        n, stim, sp = pin.get("name",""), pin.get("stimulus"), pin.get("stimulus_params") or {}
        if stim == "idc" and "dc" in sp:
            raw = str(sp["dc"]).rstrip("mAauA")
            try:
                v = float(raw)
                unit = "m" if sp["dc"].endswith("m") else ""
                params.append({"name":f"{n}.dc","target":"pin_classifications",
                    "path":f"pins[{n}].stimulus_params.dc","low":round(v*0.5,4),
                    "high":round(v*2.0,4),"dtype":"float","unit":unit})
            except ValueError: pass
    cc = sum(1 for p in params if p.get("target")=="circuit")
    sc = sum(1 for p in params if p.get("target")=="pin_classifications")
    print(f"[config] Generated {len(params)} params: {cc} circuit + {sc} stimulus")
    return {"params":params,"max_iterations":30,"n_init":0,"seed":42,"spec_weights":{}}


if __name__ == "__main__":
    import numpy as np
    parser = argparse.ArgumentParser(description="SMIC180 Circuit Parameter Optimizer")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-iter", type=int, default=None)
    parser.add_argument("--generate-config", action="store_true")
    parser.add_argument("--topology-config", action="store_true",
        help="Generate config using topology-aware parameter selection")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--total-timeout", type=int, default=0)
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    if args.generate_config:
        cfg = generate_default_config(run_dir)
        out = run_dir / "optimization" / "optimizer_config.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(cfg, indent=2, ensure_ascii=False)); sys.exit(0)
    if args.topology_config:
        from virtuoso_bridge import VirtuosoClient
        from sim_io.flow import load_dut_context as _ldc
        client_tc = VirtuosoClient.from_env()
        dut_tc = _ldc(run_dir)
        cfg = generate_topology_config(client_tc, dut_tc.lib, dut_tc.primary_cell, run_dir,
            spec_weights={
                "VOUT": {"gain_db_min": 60, "bandwidth_min": 100000, "phase_margin_min": 45},
                "AVD": {"p_max": 0.0005}
            })
        out = run_dir / "optimization" / "optimizer_config_topology.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(cfg, indent=2, ensure_ascii=False)); sys.exit(0)
    if args.config:
        raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
    else:
        raw = generate_default_config(run_dir)
    specs = raw.get("params", [])
    spec_weights = raw.get("spec_weights", {})
    n = len(specs)
    if n == 0: print("ERROR: no tunable parameters", file=sys.stderr); sys.exit(1)
    # Convert SI string bounds (e.g. "600n" -> 6e-7) for numpy
    def _to_float(val):
        if isinstance(val, (int, float)):
            return float(val)
        return _parse_si_param(str(val)) or 0.0
    lb = np.array([_to_float(s["low"]) for s in specs], dtype=float)
    ub = np.array([_to_float(s["high"]) for s in specs], dtype=float)
    names = [s["name"] for s in specs]
    if args.max_iter is not None: raw["max_iterations"] = args.max_iter
    engine_cfg = EngineConfig(n_params=n, lb=lb, ub=ub, param_names=names,
        max_iterations=raw.get("max_iterations", 30), n_init=raw.get("n_init", 0),
        seed=raw.get("seed", 42), batch_size=args.batch_size, total_timeout=args.total_timeout)
    base_data = json.loads((run_dir / "pin_classifications.json").read_text(encoding="utf-8"))
    opt_dir = run_dir / "optimization"
    opt_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*60}\n SMIC180 Circuit Optimizer: {n} params, {engine_cfg.max_iterations} iters\n{'='*60}\n")
    from virtuoso_bridge import VirtuosoClient
    from sim_io.flow import load_dut_context
    client = VirtuosoClient.from_env()
    dut = load_dut_context(run_dir)
    lib, tb = dut.lib, dut.tb_cell
    circuit_cell = dut.primary_cell  # transistors live in primary_cell, not _tb
    backup_schematic(client, lib, circuit_cell, run_dir=run_dir)
    per_eval_timeout = args.timeout
    _specs_snapshot = list(specs)
    _best_holder = {"iter": 0, "params": {}, "obj": float("inf")}
    def objective(x_norm):
        xn = x_norm * (ub - lb) + lb
        trial = copy.deepcopy(base_data)
        updated_specs = copy.deepcopy(_specs_snapshot)
        for i, s in enumerate(updated_specs):
            raw_val = float(xn[i])
            if s.get("target") == "circuit":
                if s.get("cdf_param") == "m":
                    s["_current_value"] = str(int(round(raw_val)))
                elif s.get("cdf_param") == "fingers":
                    s["_current_value"] = str(int(round(raw_val)))
                elif s.get("cdf_param") == "l":
                    # L has a minimum in SMIC180 PDK (typically 180n)
                    min_l = _parse_si_param(s.get("low", "180n")) or 180e-9
                    s["_current_value"] = _scale_si(max(raw_val, min_l), 1.0)
                else:
                    s["_current_value"] = _scale_si(raw_val, 1.0)
            else:
                _write_param(trial, s["name"], s["path"], s.get("target","pin_classifications"),
                    raw_val, s.get("dtype","float"), s.get("unit",""))
        m = evaluate_smic180(run_dir, trial, updated_specs, dry_run=args.dry_run, timeout=per_eval_timeout)
        err = m.pop("_error", None)
        if err: print(f"  ERROR: {err}"); return 1e6
        return compute_spec_violation(m, spec_weights)
    def on_best(it, p, o):
        _best_holder["iter"] = it; _best_holder["params"] = p; _best_holder["obj"] = o
        print(f"  * New best at {it}: violation={o:.6f}")
    def on_eval(it, p, o):
        if o < 1e6: print(f"  Iter {it}: violation={o:.6f}")
        hp = opt_dir / "optimization_history.json"
        hist = json.loads(hp.read_text(encoding="utf-8")) if hp.exists() else []
        hist.append({"iteration":it,"params":p,"objective":o,"timestamp":time.strftime("%Y-%m-%dT%H:%M:%S")})
        tmp_hp = hp.with_suffix('.json.tmp')
        tmp_hp.write_text(json.dumps(hist, indent=2, default=str), encoding="utf-8")
        tmp_hp.replace(hp)
    try:
        result = run_optimization_loop(objective, engine_cfg, on_eval=on_eval, on_best=on_best)
    finally:
        # Save optimized version BEFORE restoring original
        # We need to re-apply best params to TB, save as _opt, then restore
        if result and result.get("best_params"):
            best = result["best_params"]
            print(f"\n[optimizer] Writing best params to schematic...")
            # Re-apply best params
            updated_specs_best = copy.deepcopy(_specs_snapshot)
            for i, s in enumerate(updated_specs_best):
                raw_val = float(best.get(s["name"], lb[i]))
                if s.get("target") == "circuit":
                    if s.get("cdf_param") == "m":
                        s["_current_value"] = str(int(round(raw_val)))
                    elif s.get("cdf_param") == "fingers":
                        s["_current_value"] = str(int(round(raw_val)))
                    elif s.get("cdf_param") == "l":
                        min_l = _parse_si_param(s.get("low", "180n")) or 180e-9
                        s["_current_value"] = _scale_si(max(raw_val, min_l), 1.0)
                    else:
                        s["_current_value"] = _scale_si(raw_val, 1.0)
            for s in updated_specs_best:
                if s.get("target") == "circuit" and s.get("_current_value"):
                    set_instance_params_headless(client, lib, circuit_cell, s["instance"], {s["cdf_param"]: s["_current_value"]})
            _sch_check_and_save(client, lib, circuit_cell)
            # Save as _opt copy
            save_optimized_schematic(client, lib, circuit_cell, run_dir=run_dir)
        # Restore original
        print(f"[optimizer] Restoring original schematic...")
        restore_schematic(client, lib, circuit_cell, run_dir=run_dir)
    # Recover best result from _best_holder or history if result is empty
    if result and not result.get("best_params") and _best_holder.get("params"):
        result["best_params"] = _best_holder["params"]
        result["best_iteration"] = _best_holder["iter"]
        result["best_objective"] = _best_holder["obj"]
        print(f"  Recovered best from callbacks: iter={_best_holder["iter"]}, obj={_best_holder["obj"]:.6f}")
    # Also try recovering from history file
    hp = opt_dir / "optimization_history.json"
    if hp.exists() and (not result or not result.get("best_params")):
        hist = json.loads(hp.read_text(encoding="utf-8"))
        non_err = [e for e in hist if e.get("objective", 1e6) < 1e6]
        if non_err:
            best_h = min(non_err, key=lambda x: x["objective"])
            result = result or {}
            result["best_params"] = best_h["params"]
            result["best_iteration"] = best_h["iteration"]
            result["best_objective"] = best_h["objective"]
            result["total_iterations"] = len(hist)
            print(f"  Recovered best from history: iter={best_h["iteration"]}, obj={best_h["objective"]:.6f}")
    _brp = (opt_dir / "best_result.json")
    _brp_tmp = _brp.with_suffix('.json.tmp')
    _brp_tmp.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    _brp_tmp.replace(_brp)
    print(f"\n{'='*60}\n Done: iter {result['best_iteration']}, violation={result['best_objective']:.6f}\n Params: {result['best_params']}\n{'='*60}\n")

"""
Simulation Run Pipeline — Netlist Export → Spectre Execution → Result Parsing.

Step 3a: export_netlist()  — si batch netlist export from _tb schematic
Step 3b: build_sim_deck() — append model include + analysis + options
Step 3c: run_spectre()    — wrapper around SpectreSimulator
Step 3d: parse_results()  — measurement extraction from PSF data
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.models import ExecutionStatus, SimulationResult
from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

from sim_io.sim.deck import build_sim_deck_from_file
from sim_io.sim.config import (
    SimDeckConfig, summarize_netlist, write_sim_config_input,
    resolve_sim_config,
)
from sim_io.site_config import SiteConfig
from sim_io.pin_types import PinInfo, classify_pin_heuristic, load_pin_classifications
from sim_io.config import create_run_dir

_SIM_IO = Path(__file__).resolve().parents[2]

# Remote directory for si batch netlist export
_SI_REMOTE_DIR = "/tmp/sim_io_si_run"

# si.env template path
_SI_ENV_TEMPLATE = _SIM_IO / "templates" / "si_spectre.env"


def _spectre_run_dir(run_dir: Path) -> Path:
    path = run_dir / "spectre"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_psf_dir(run_dir: Path, deck_path: Path) -> Path:
    candidates = [
        deck_path.parent / f"{deck_path.stem}.raw",
        run_dir / f"{deck_path.stem}.raw",
    ]
    for psf_dir in candidates:
        if not psf_dir.exists():
            nested = psf_dir / psf_dir.name
            if nested.exists():
                return nested
            continue
        return psf_dir
    return candidates[0]


def _load_primary_psf_data(run_dir: Path, deck_path: Path) -> dict:
    """Load measurement data with SIM-IO's parser instead of Bridge internals."""
    try:
        from sim_io.sim.viz import DCSweepData, NoiseData, OperatingPointData, TranData, parse_psf_ascii
    except Exception:
        return {}

    psf_dir = _resolve_psf_dir(run_dir, deck_path)
    if not psf_dir.exists():
        return {}

    files = [p for p in sorted(psf_dir.glob("*")) if p.is_file()]
    tran_files = [p for p in files if "tran" in p.name.lower()]
    dc_files = [p for p in files if "dc" in p.name.lower()]
    ac_files = [p for p in files if "ac" in p.name.lower()]
    noise_files = [p for p in files if "noise" in p.name.lower()]
    op_files = [p for p in files if "dcop" in p.name.lower() or p.name.lower().startswith("op.") or p.name.lower() == "opinfo.info"]

    # Load all PSF files into a single merged dict
    merged = {}
    has_tran = False
    for psf_file in tran_files:
        try:
            parsed = parse_psf_ascii(psf_file)
        except Exception:
            continue
        if isinstance(parsed, TranData):
            merged.update({"time": parsed.time, **parsed.signals})
            has_tran = True
            break  # Tran takes priority — don't load DC/AC if tran exists

    if not has_tran:
        for psf_file in dc_files:
            try:
                parsed = parse_psf_ascii(psf_file)
            except Exception:
                continue
            if isinstance(parsed, DCSweepData):
                merged.update({parsed.sweep_var: parsed.sweep_values, **parsed.signals})

        for psf_file in ac_files:
            try:
                parsed = parse_psf_ascii(psf_file)
            except Exception:
                continue
            try:
                from sim_io.sim.viz import ACSweepData
                if isinstance(parsed, ACSweepData):
                    merged["freq"] = parsed.freq
                    for sig_name, values in parsed.signals.items():
                        merged[f"ac:{sig_name}"] = values
            except Exception:
                continue

        for psf_file in noise_files:
            try:
                parsed = parse_psf_ascii(psf_file)
            except Exception:
                continue
            if isinstance(parsed, NoiseData):
                merged["noise_freq"] = parsed.freq
                merged.update({f"noise:{name}": values for name, values in parsed.signals.items()})

        for psf_file in op_files:
            try:
                parsed = parse_psf_ascii(psf_file)
            except Exception:
                continue
            if isinstance(parsed, OperatingPointData):
                merged.update({f"op:{name}": fields for name, fields in parsed.instances.items()})

    return merged


# ── Template Helpers ────────────────────────────────────────────

def _load_si_env_template() -> str:
    """Load the si.env template from SIM-IO/templates/si_spectre.env."""
    if not _SI_ENV_TEMPLATE.is_file():
        raise FileNotFoundError(f"si.env template not found: {_SI_ENV_TEMPLATE}")
    return _SI_ENV_TEMPLATE.read_text(encoding="utf-8")


def _substitute_si_env(template: str, *, library: str, top_cell: str, run_dir: str) -> str:
    """Replace @PLACEHOLDER@ patterns in the si.env template."""
    return (
        template
        .replace("@LIBRARY@", library)
        .replace("@TOP_CELL@", top_cell)
        .replace("@SI_RUN_DIR@", run_dir)
    )



# ── Spectre CSHRC Auto-generation ───────────────────────────────

_CSHRC_REMOTE_PATH = "/tmp/sim_io_spectre_setup.csh"


def ensure_spectre_cshrc(site: SiteConfig, client: VirtuosoClient) -> str:
    """Auto-generate and upload a cshrc for Spectre execution.

    Reads SiteConfig for MMSIM_ROOT and license vars, writes a complete cshrc
    to /tmp/sim_io_spectre_setup.csh on the remote, and sets VB_CADENCE_CSHRC.

    Returns the remote cshrc path.
    """
    mmsim_root = site.mmsim_root
    lm_license = site.lm_license_file
    cds_lic = site.cds_lic_file
    ic_root = site.ic_root

    missing = []
    if not mmsim_root:
        missing.append("cadence.mmsim_root")
    if not lm_license:
        missing.append("spectre.lm_license_file")
    if not cds_lic:
        missing.append("spectre.cds_lic_file")
    if missing:
        raise RuntimeError(
            "Missing required site config for Spectre: "
            + ", ".join(missing)
            + ". Set these in _local/site.yaml."
        )

    # Build cshrc content
    lines = ["#!/bin/csh", "# Auto-generated by SIM-IO for Spectre execution", ""]
    if mmsim_root:
        lines.append(f"# MMSIM tools")
        lines.append(f"setenv PATH {{{mmsim_root}/tools/bin:{mmsim_root}/tools/bin/64bit}}:$PATH")
        lines.append(f"setenv LD_LIBRARY_PATH {{{mmsim_root}/tools/lib/64bit}}:$LD_LIBRARY_PATH")
    if ic_root:
        lines.append(f"# IC tools (Virtuoso)")
        lines.append(f"setenv PATH {{{ic_root}/tools/bin:{ic_root}/tools/dfII/bin:{ic_root}/tools/bin/64bit}}:$PATH")
        lines.append(f"setenv LD_LIBRARY_PATH {{{ic_root}/tools/lib/64bit:{ic_root}/tools/dfII/lib/64bit}}:$LD_LIBRARY_PATH")
    if lm_license:
        lines.append(f"# License")
        lines.append(f'setenv LM_LICENSE_FILE "{lm_license}"')
    if cds_lic:
        lines.append(f'setenv CDS_LIC_FILE "{cds_lic}"')
    lines.append("")  # trailing newline

    cshrc_content = "\n".join(lines)

    def _verify_cshrc_exists() -> bool:
        if tunnel is not None:
            check = tunnel.run_command(
                f"test -s {shlex.quote(_CSHRC_REMOTE_PATH)}",
                timeout=15,
            )
            return check.returncode == 0
        try:
            result = client.execute_skill(
                f'isFile("{_CSHRC_REMOTE_PATH}")',
                timeout=10,
            )
            return bool(result.output and "t" in result.output.lower())
        except Exception:
            return False

    # Upload to remote via tunnel
    tunnel = client._tunnel
    if tunnel is not None:
        try:
            tunnel.upload_text(cshrc_content, _CSHRC_REMOTE_PATH)
            print(f"[step3c] Uploaded spectre cshrc to {_CSHRC_REMOTE_PATH}")
        except Exception as e:
            print(f"[step3c] WARNING: Failed to upload cshrc: {e}")
    else:
        # Fallback: write via SKILL shell
        try:
            escaped = cshrc_content.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
            client.execute_skill(f'sh("cat > {_CSHRC_REMOTE_PATH} << \'CSHRC_EOF\'\\n{cshrc_content}CSHRC_EOF")')
            print(f"[step3c] Wrote spectre cshrc via SKILL to {_CSHRC_REMOTE_PATH}")
        except Exception as e:
            print(f"[step3c] WARNING: Failed to write cshrc via SKILL: {e}")

    if not _verify_cshrc_exists():
        print(f"[step3c] WARNING: Spectre cshrc missing after upload; retrying {_CSHRC_REMOTE_PATH}")
        if tunnel is not None:
            tunnel.upload_text(cshrc_content, _CSHRC_REMOTE_PATH)
        else:
            client.execute_skill(
                f'sh("cat > {_CSHRC_REMOTE_PATH} << \'CSHRC_EOF\'\\n{cshrc_content}CSHRC_EOF")',
                timeout=20,
            )
        if not _verify_cshrc_exists():
            raise RuntimeError(f"Spectre cshrc upload verification failed: {_CSHRC_REMOTE_PATH}")

    # Set VB_CADENCE_CSHRC so SpectreSimulator.from_env() picks it up
    os.environ["VB_CADENCE_CSHRC"] = _CSHRC_REMOTE_PATH
    print(f"[step3c] Set VB_CADENCE_CSHRC={_CSHRC_REMOTE_PATH}")

    return _CSHRC_REMOTE_PATH


# ── cds.lib Helper ──────────────────────────────────────────────

_ESSENTIAL_LIBS = {
    "analogLib": None,  # path auto-discovered from Virtuoso
    "basic": None,
}


def _ensure_cds_lib(cds_lib_path: str, client: VirtuosoClient, tunnel) -> None:
    """Ensure cds.lib includes analogLib and basic definitions.

    si batch netlister needs these for vsource, capacitor, gnd, noConn, etc.
    If the cds.lib chain doesn't include them, append DEFINE lines.
    """
    # Check which essential libs are already resolvable
    missing = []
    for lib_name in _ESSENTIAL_LIBS:
        r = client.execute_skill(f'ddGetObj("{lib_name}")', timeout=10)
        if not r.output or "nil" in (r.output or "").lower():
            missing.append(lib_name)

    if not missing:
        return

    print(f"[step3a] cds.lib missing: {missing}. Appending DEFINE lines.")

    # Discover paths for missing libs from Virtuoso's search paths
    lib_paths = {}
    if "analogLib" in missing:
        r = client.execute_skill('ddGetObjReadPath(ddGetObj("analogLib"))', timeout=10)
        # If that fails, use IC root
        if r.errors or not r.output or "nil" in (r.output or "").lower():
            # Try to find analogLib under IC root
            ic_root = os.getenv("SIM_IC_ROOT", "")
            if ic_root:
                lib_paths["analogLib"] = f"{ic_root}/tools/dfII/etc/cdslib/analogLib"
    if "basic" in missing:
        ic_root = os.getenv("SIM_IC_ROOT", "")
        if ic_root:
            lib_paths["basic"] = f"{ic_root}/tools/dfII/etc/cdslib/basic"

    # Append DEFINE lines to cds.lib
    define_lines = []
    for lib_name in missing:
        path = lib_paths.get(lib_name, "")
        if path:
            define_lines.append(f'DEFINE {lib_name} {path}')

    if not define_lines:
        print(f"[step3a] WARNING: Cannot determine paths for {missing}. Netlist may fail.")
        return

    append_text = "\n# Auto-added by SIM-IO for si netlister\n" + "\n".join(define_lines) + "\n"

    if tunnel is not None:
        try:
            tunnel.run_command(f'echo \'{append_text}\' >> {cds_lib_path}', timeout=15)
            print(f"[step3a] Appended to {cds_lib_path}: {define_lines}")
        except Exception as e:
            print(f"[step3a] WARNING: Failed to append to cds.lib: {e}")
    else:
        try:
            client.execute_skill(f'sh("echo \'{append_text}\' >> {cds_lib_path}")')
            print(f"[step3a] Appended to {cds_lib_path}: {define_lines}")
        except Exception as e:
            print(f"[step3a] WARNING: Failed to append to cds.lib via SKILL: {e}")


# ── Step 3a: Netlist Export ────────────────────────────────────

def export_netlist(
    client: VirtuosoClient,
    lib: str,
    tb_cell: str,
    run_dir: Path,
    *,
    site: SiteConfig,
) -> Optional[Path]:
    """Export Spectre netlist from _tb schematic via si batch netlister.

    Uses SiteConfig for cds.lib, IC root, and license vars.
    License vars fall back to SKILL auto-discovery from Virtuoso.

    Steps:
      0. schCheck + dbSave on the _tb schematic (si requires it)
      1. Create remote run directory
      2. Write si.env from template with placeholder substitution
      3. Resolve license vars (SiteConfig > SKILL discovery)
      4. Run si -batch -command nl with user's cds.lib
      5. Download netlist to local run_dir

    Returns the path to the downloaded netlist file, or None on failure.
    """
    print(f"[step3a] Exporting netlist for {lib}/{tb_cell}")

    # 0. schCheck + dbSave — si refuses to netlist if cellview is modified
    r = client.execute_skill(
        f'let((cv) cv = dbOpenCellViewByType("{lib}" "{tb_cell}" "schematic" "schematic" "a") '
        f'schCheck(cv) dbSave(cv) dbClose(cv) t)'
    )
    if r.errors:
        print(f"[step3a] WARNING: schCheck/save failed: {r.errors}")

    # 1. Create remote run directory
    r = client.execute_skill(f'sh("mkdir -p {_SI_REMOTE_DIR}")')

    # 1.5. Clean stale output from previous runs
    tunnel = client._tunnel
    if tunnel is not None:
        try:
            tunnel.run_command(f"rm -rf {_SI_REMOTE_DIR}/*", timeout=15)
        except Exception as e:
            print(f"[step3a] WARNING: Failed to clean remote si dir: {e}")
    else:
        client.execute_skill(f'sh("rm -rf {_SI_REMOTE_DIR}/*")')

    # 1.6. Ensure cds.lib includes analogLib and basic definitions
    # These are required by si for vsource, capacitor, gnd, noConn etc.
    _ensure_cds_lib(site.cds_lib, client, tunnel)

    # 2. Write si.env from template
    template = _load_si_env_template()
    si_env_content = _substitute_si_env(
        template,
        library=lib,
        top_cell=tb_cell,
        run_dir=_SI_REMOTE_DIR,
    )

    # NOTE: simInitEnvWithArgs() is intentionally SKIPPED because:
    #   1. It triggers a blocking dialog in Virtuoso (Problem 7)
    #   2. It times out after 30s (Problem 10)
    #   3. We overwrite si.env with our own template anyway
    # The directory is already created above, so we just upload our si.env.

    tunnel = client._tunnel
    if tunnel is not None:
        tunnel.upload_text(si_env_content, f"{_SI_REMOTE_DIR}/si.env")
    else:
        client.execute_skill(
            f'csh("echo \'{si_env_content}\' > {_SI_REMOTE_DIR}/si.env")'
        )

    # 3. Resolve license vars from site config.
    license_env: dict[str, str] = {}
    if site.lm_license_file:
        license_env["LM_LICENSE_FILE"] = site.lm_license_file
    if site.cds_lic_file:
        license_env["CDS_LIC_FILE"] = site.cds_lic_file

    missing = [v for v in ("LM_LICENSE_FILE", "CDS_LIC_FILE") if v not in license_env]
    if missing:
        raise RuntimeError(
            "Missing required site config for netlist export: "
            + ", ".join(missing)
            + ". Set spectre.lm_license_file and spectre.cds_lic_file in _local/site.yaml."
        )

    print(f"[step3a] License: LM_LICENSE_FILE={license_env.get('LM_LICENSE_FILE', '(missing)')}, "
          f"CDS_LIC_FILE={license_env.get('CDS_LIC_FILE', '(missing)')}")

    # 3.5. Pre-flight: verify essential libraries are resolvable via cds.lib
    for _essential_lib in ("analogLib", "basic"):
        r_check = client.execute_skill(
            f'ddGetObj("{_essential_lib}")', timeout=10
        )
        if r_check.output and "nil" in r_check.output:
            print(f"[step3a] WARNING: Library '{_essential_lib}' not found in cds.lib. "
                  f"Netlist export may fail. Check cds.lib: {site.cds_lib}")

    # 4. Run si batch netlister via SSH shell
    ic_root = site.ic_root
    export_lines = [
        f"export IC_HOME={ic_root}",
        f"export CDSHOME={ic_root}",
        f"export PATH={ic_root}/tools/bin:{ic_root}/tools/dfII/bin:{ic_root}/tools/bin/64bit:$PATH",
        f"export LD_LIBRARY_PATH={ic_root}/tools/lib/64bit:{ic_root}/tools/dfII/lib/64bit:$LD_LIBRARY_PATH",
    ]
    for var, val in license_env.items():
        export_lines.append(f"export {var}={val}")

    env_setup = "; ".join(export_lines)
    si_cmd = (
        f'{env_setup}; '
        f'cd {_SI_REMOTE_DIR}; '
        f'{site.si_bin} -batch -cdslib {site.cds_lib} -command nl 2>&1 | tail -20'
    )

    if tunnel is not None:
        r = tunnel.run_command(si_cmd, timeout=300)
        output = r.stdout or ""
        if "ERROR" in output:
            print(f"[step3a] si output:\n{output}")
    else:
        r = client.run_shell_command(
            f'cd {_SI_REMOTE_DIR}; {site.si_bin} -batch -cdslib {site.cds_lib} -command nl',
            timeout=300,
        )
        if r.errors:
            print(f"[step3a] WARNING: si returned errors: {r.errors}")

    # 5. Download netlist
    spectre_dir = _spectre_run_dir(run_dir)
    local_netlist = spectre_dir / "netlist.scs"
    r = client.download_file(f"{_SI_REMOTE_DIR}/netlist", str(local_netlist))
    if not r.ok:
        # Try alternate output location
        alt_path = f"{_SI_REMOTE_DIR}/netlist/netlist"
        r = client.download_file(alt_path, str(local_netlist))

    if not local_netlist.exists():
        print(f"[step3a] ERROR: Failed to download netlist")
        return None

    print(f"[step3a] Netlist exported: {local_netlist} ({local_netlist.stat().st_size} bytes)")
    return local_netlist


# ── Step 3b: Build Sim Deck ────────────────────────────────────

def build_deck(
    netlist_path: Path,
    config,
    run_dir: Path,
) -> Path:
    """Build a complete Spectre deck from si netlist + config.

    Accepts SimDeckConfig.
    Returns the path to the complete deck file.
    """
    deck_text = build_sim_deck_from_file(netlist_path, config)
    spectre_dir = _spectre_run_dir(run_dir)
    deck_path = spectre_dir / "deck.scs"
    deck_path.write_text(deck_text, encoding="utf-8")
    print(f"[step3b] Deck built: {deck_path}")
    return deck_path


# ── Step 3c: Run Spectre ──────────────────────────────────────
# -- Step 3a.25: Patch Netlist from Pin Classifications --------

_CDF_TO_SPECTRE_PARAM = {
    "acm": "mag",
    "acp": "phase",
    "acmag": "mag",
}


def _resolve_param_value_local(value: str, vdd_value: float) -> str:
    if "VDD" not in value:
        return value
    result = value.replace("VDD", str(vdd_value))
    if "/" in result:
        parts = result.split("/")
        if len(parts) == 2:
            try:
                val = float(parts[0]) / float(parts[1])
                return f"{val:g}"
            except (ValueError, ZeroDivisionError):
                pass
    return result


def _patch_netlist_from_classifications(
    netlist_path: Path,
    run_dir: Path,
    client: VirtuosoClient,
) -> None:
    classif_path = run_dir / "pin_classifications.json"
    if not classif_path.exists():
        print("[step3a.25] No pin_classifications.json; skipping netlist patch")
        return

    try:
        classif_data = json.loads(classif_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[step3a.25] WARNING: Failed to read pin_classifications: {e}")
        return

    pins = classif_data.get("pins", [])
    vdd_value = classif_data.get("vdd_value", 1.8)

    patches: dict[str, dict[str, str]] = {}

    for pin in pins:
        pin_name = pin.get("name", "")
        dc = pin.get("device_class", "")
        domain = pin.get("domain", "")

        if domain != "analog":
            continue

        if dc in ("analog_input", "analog_power") and pin.get("stimulus_params"):
            inst_name = f"SRC_{pin_name}"
            params = {}
            for key, val in pin["stimulus_params"].items():
                spectre_key = _CDF_TO_SPECTRE_PARAM.get(key, key)
                resolved = _resolve_param_value_local(str(val), vdd_value)
                params[spectre_key] = resolved
            patches[inst_name] = params

        if dc == "bias_current" and pin.get("stimulus_params"):
            inst_name = f"SRC_{pin_name}"
            params = {}
            for key, val in pin["stimulus_params"].items():
                spectre_key = _CDF_TO_SPECTRE_PARAM.get(key, key)
                resolved = _resolve_param_value_local(str(val), vdd_value)
                params[spectre_key] = resolved
            patches[inst_name] = params

        if dc == "analog_output":
            load_inst = f"LOAD_{pin_name}"
            load_p = pin.get("load_params") or {}
            stim_p = pin.get("stimulus_params") or {}
            merged = {**stim_p, **load_p}
            if merged:
                params = {k: str(v) for k, v in merged.items()}
                patches[load_inst] = params

    ground_pin_pvss = classif_data.get("analog_local_grounds", [])
    for ground_info in ground_pin_pvss:
        pvss_name = ground_info.get("pvss_name") or ground_info.get("name", "")
        if pvss_name:
            patches[f"PVSS_{pvss_name}"] = {"dc": "0"}

    if not patches:
        print("[step3a.25] No patches needed")
        return

    netlist_text = netlist_path.read_text(encoding="utf-8")
    lines = netlist_text.split("\n")
    patched_count = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue

        m = re.match(r'^(\S+)\s+\([^)]+\)\s+(vsource|isource|capacitor)\b', stripped)
        if not m:
            continue

        inst = m.group(1)
        dev_type = m.group(2)

        if inst not in patches:
            continue

        params = patches[inst]
        param_parts = []

        if dev_type in ("vsource", "isource"):
            has_type_dc = "type=dc" in stripped
            if not has_type_dc:
                param_parts.append("type=dc")
            for key, val in params.items():
                if key == "type":
                    continue
                line_re = re.compile(r'\b' + re.escape(key) + r'=[^\s,]+')
                lines[i] = line_re.sub('', lines[i])
                param_parts.append(f"{key}={val}")

        elif dev_type == "capacitor":
            for key, val in params.items():
                line_re = re.compile(r'\b' + re.escape(key) + r'=[^\s,]+')
                lines[i] = line_re.sub('', lines[i])
                param_parts.append(f"{key}={val}")

        if param_parts:
            lines[i] = lines[i].rstrip() + " " + " ".join(param_parts)
            patched_count += 1
            print(f"[step3a.25] Patched {inst}: {' '.join(param_parts)}")

    if patched_count > 0:
        netlist_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[step3a.25] Patched {patched_count} instances in netlist")
    else:
        print("[step3a.25] No instances matched for patching")


def run_spectre(
    deck_path: Path,
    run_dir: Path,
    *,
    spectre_cmd: str = "",
    mode: str = "spectre",
    timeout: int = 600,
    site: Optional[SiteConfig] = None,
    client: Optional[VirtuosoClient] = None,
) -> SimulationResult:
    """Run Spectre simulation on a complete deck.

    Wraps SpectreSimulator.from_env() — handles local vs remote automatically.
    Uses explicit site configuration for Spectre command and license setup.

    If ``site`` and ``client`` are provided, auto-generates the spectre
    cshrc on the remote (including MMSIM path + license vars) and uses
    ``site.spectre_bin_path`` as the default spectre command.
    """
    # Auto-generate cshrc for spectre if site/client are available
    if site is not None and client is not None:
        ensure_spectre_cshrc(site, client)

    # Resolve spectre command: explicit arg > SPECTRE_CMD env > SiteConfig.
    if not spectre_cmd:
        spectre_cmd = os.getenv("SPECTRE_CMD", "")
    if not spectre_cmd and site is not None and site.spectre_bin_path:
        spectre_cmd = site.spectre_bin_path
        print(f"[step3c] Using spectre from SiteConfig: {spectre_cmd}")
    if not spectre_cmd:
        raise RuntimeError(
            "Spectre command is not configured. Add cadence.mmsim_root to "
            "_local/site.yaml or pass --spectre-cmd/SPECTRE_CMD explicitly."
        )

    print(f"[step3c] Running Spectre (mode={mode}, timeout={timeout}s)")
    print(f"[step3c] Command: {spectre_cmd}")

    sim = SpectreSimulator.from_env(
        spectre_cmd=spectre_cmd,
        spectre_args=spectre_mode_args(mode),
        timeout=timeout,
        work_dir=deck_path.parent,
        output_format="psfascii",
    )

    result = sim.run_simulation(deck_path, {})
    primary_data = _load_primary_psf_data(run_dir, deck_path)
    if primary_data:
        result.data = primary_data

    if result.ok:
        signals = list(result.data.keys())
        print(f"[step3c] Spectre OK — {len(signals)} signals")
    else:
        print(f"[step3c] Spectre FAILED: {result.errors[:3]}")

    # Bridge may classify "Warning from spectre during circuit read-in" as a
    # netlist read error even when Spectre exits successfully. SIM-IO treats
    # result.ok as the source of truth for the public run summary.
    summary_errors = [] if result.ok else result.errors

    result.metadata["summary"] = {
        "status": result.status.value,
        "tool_version": result.tool_version,
        "errors": summary_errors,
        "warnings": result.warnings[:5],
        "num_signals": len(result.data) if result.data else 0,
    }

    return result


# ── Step 3d: Parse Results ────────────────────────────────────

def _measure_tran(data: dict, signal: str) -> dict:
    """Extract key metrics from a transient signal."""
    try:
        import numpy as np
    except ImportError:
        # Fallback without numpy
        values = data.get(signal, [])
        if not values:
            return {"signal": signal, "error": "no data"}
        vmax = max(values)
        vmin = min(values)
        return {
            "signal": signal,
            "vmax": vmax,
            "vmin": vmin,
            "vavg": sum(values) / len(values),
            "vpp": vmax - vmin,
        }

    time = np.array(data.get("time", []), dtype=float)
    v = np.array(data.get(signal, []), dtype=float)
    if len(v) == 0:
        return {"signal": signal, "error": "no data"}

    metrics = {
        "signal": signal,
        "vmax": float(np.max(v)),
        "vmin": float(np.min(v)),
        "vavg": float(np.mean(v)),
        "vpp": float(np.max(v) - np.min(v)),
    }

    # Slew rate (rising): dv/dt between 10% and 90% of vpp
    vpp = metrics["vpp"]
    if vpp > 0 and len(time) > 1:
        v_lo = metrics["vmin"] + 0.1 * vpp
        v_hi = metrics["vmin"] + 0.9 * vpp
        # Find first rising edge crossing 10% and 90%
        rising = False
        t_lo, t_hi = None, None
        for i in range(1, len(v)):
            if not rising and v[i] >= v_lo and v[i - 1] < v_lo:
                t_lo = float(time[i])
                rising = True
            if rising and v[i] >= v_hi:
                t_hi = float(time[i])
                break
        if t_lo is not None and t_hi is not None and t_hi > t_lo:
            dt = t_hi - t_lo
            metrics["slew_rate"] = 0.8 * vpp / dt

    return metrics


def _measure_dc(data: dict, signal: str, vdd: float = 1.8) -> dict:
    """Extract key metrics from a DC sweep signal.

    Finds the sweep variable (first non-signal key) and extracts
    voltage at nominal VDD, voltage range, and DC gain.
    """
    try:
        import numpy as np
    except ImportError:
        values = data.get(signal, [])
        if not values:
            return {"signal": signal, "error": "no data"}
        return {
            "signal": signal,
            "v_at_vdd": None,
            "vmin": min(values),
            "vmax": max(values),
            "vrange": max(values) - min(values),
        }

    v = np.array(data.get(signal, []), dtype=float)
    if len(v) == 0:
        return {"signal": signal, "error": "no data"}

    # Find sweep variable — first key that isn't the signal or "time"
    sweep_key = None
    for key in data:
        if key not in (signal, "time"):
            sweep_key = key
            break

    metrics: dict = {
        "signal": signal,
        "vmin": float(np.min(v)),
        "vmax": float(np.max(v)),
        "vrange": float(np.max(v) - np.min(v)),
    }

    if sweep_key:
        sweep = np.array(data.get(sweep_key, []), dtype=float)
        if len(sweep) == len(v) and vdd > 0:
            idx = np.argmin(np.abs(sweep - vdd))
            metrics["v_at_vdd"] = float(v[idx])
            # DC gain: dVout/dVdd near operating point
            if len(v) > 2:
                window = max(1, len(v) // 20)
                lo = max(0, idx - window)
                hi = min(len(v), idx + window + 1)
                dv = v[hi - 1] - v[lo]
                ds = sweep[hi - 1] - sweep[lo]
                if abs(ds) > 1e-12:
                    metrics["dc_gain"] = float(dv / ds)

    return metrics


def _measure_power(data: dict, v_signal: str, i_signal: str) -> dict:
    """Compute power from voltage and current waveforms.

    Returns average, peak, and static/dynamic power breakdown.
    Static power uses the DC operating point (first sample or average
    of the first 10% of the transient).
    """
    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy required for power calculation"}

    v = np.array(data.get(v_signal, []), dtype=float)
    i = np.array(data.get(i_signal, []), dtype=float)
    if len(v) == 0 or len(i) == 0:
        return {"error": "missing voltage or current data"}

    n = min(len(v), len(i))
    v, i = v[:n], i[:n]
    p = v * i

    # Static power: average of first 10% (before switching activity)
    n_static = max(1, n // 10)
    p_static = float(np.mean(np.abs(p[:n_static])))

    p_avg = float(np.mean(np.abs(p)))
    p_max = float(np.max(np.abs(p)))

    return {
        "pavg": p_avg,
        "pmax": p_max,
        "pstatic": p_static,
        "pdynamic": p_avg - p_static if p_avg > p_static else 0.0,
    }


def _normal_signal_name(name: str) -> str:
    return name.strip().lstrip("/").replace("/", ".")


def _find_signal(data: dict, name: str, dut_instance: str = "DUT") -> str | None:
    """Find a signal in PSF data by trying multiple naming conventions."""
    if not name:
        return None
    candidates = [
        f"{dut_instance}.{name}",
        name,
        f"/{dut_instance}/{name}",
        f"{dut_instance}/{name}",
        f"dc_{name}",
        f"dc_{dut_instance}.{name}",
        f"dc_{dut_instance}/{name}",
    ]
    for c in candidates:
        if c in data:
            return c

    target = _normal_signal_name(name)
    for key in data:
        norm = _normal_signal_name(key.removeprefix("dc_"))
        if norm == target or norm.endswith(f".{target}"):
            return key
    return None


def _extract_dut_pin_net_map(deck_path: Path, dut_instance: str = "DUT") -> dict[str, str]:
    """Map subckt pin names to top-level TB net names for the DUT instance."""
    if not deck_path or not deck_path.exists():
        return {}
    text = deck_path.read_text(encoding="utf-8", errors="replace")

    subckt_match = re.search(r"^\s*subckt\s+(\S+)\s+(.+?)^\s*ends\s+\1\b",
                             text, flags=re.MULTILINE | re.DOTALL)
    if not subckt_match:
        return {}
    cell_name = subckt_match.group(1)
    subckt_pins = re.sub(r"\\\s*\n\s*", " ", subckt_match.group(2)).split()

    inst_match = re.search(
        rf"^\s*{re.escape(dut_instance)}\s*\((.*?)\)\s+{re.escape(cell_name)}\b",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not inst_match:
        return {}
    inst_nets = re.sub(r"\\\s*\n\s*", " ", inst_match.group(1)).split()
    return dict(zip(subckt_pins, inst_nets))


def _detect_analysis_type(data: dict) -> str:
    """Detect the primary analysis type from PSF data keys."""
    if "time" in data:
        return "tran"
    # AC data: has "freq" key and "ac:" prefixed signal keys
    if "freq" in data and any(k.startswith("ac:") for k in data):
        return "ac"
    # DC sweep: has a sweep variable but no "time"
    keys = [k for k in data if k != "time"]
    if len(keys) >= 2:
        return "dc"
    return "unknown"



def _measure_dc_op(data: dict, signal: str) -> dict:
    """Extract DC operating point metrics (single-value signals, not sweeps)."""
    vals = data.get(signal, [])
    if not vals:
        return {"error": "no data"}
    v = vals[0] if isinstance(vals, list) else vals
    return {
        "signal": signal,
        "vdc": float(v),
        "vmin": float(v),
        "vmax": float(v),
        "vrange": 0.0,
    }

def _measure_ac(data: dict, signal_key: str) -> dict:
    """Extract AC small-signal metrics from PSF data.

    Expects data to contain 'freq' list and 'ac:signal_key' list of (real,imag).
    """
    import math
    freq = data.get("freq", [])
    ac_signal = data.get(f"ac:{signal_key}", [])
    if not freq or not ac_signal:
        return {"error": "no AC data"}

    mag = [math.sqrt(r*r + i*i) for r, i in ac_signal]
    phase = [math.degrees(math.atan2(i, r)) for r, i in ac_signal]
    mag_db = [20 * math.log10(m) if m > 1e-20 else -200 for m in mag]

    metrics = {}
    # DC gain
    dc_gain = mag_db[0] if mag_db else 0
    metrics["gain_db"] = round(dc_gain, 2)
    metrics["gain_V_V"] = round(mag[0], 4) if mag else 0

    # -3dB bandwidth
    for j in range(1, len(mag_db)):
        if mag_db[j] < dc_gain - 3:
            f1, f2 = freq[j-1], freq[j]
            g1, g2 = mag_db[j-1], mag_db[j]
            if g2 != g1:
                frac = (dc_gain - 3 - g1) / (g2 - g1)
            else:
                frac = 0.5
            metrics["bandwidth"] = f1 + frac * (f2 - f1)
            break

    # Unity Gain Frequency (0dB crossing)
    for j in range(1, len(mag_db)):
        if mag_db[j-1] > 0 and mag_db[j] <= 0:
            f1, f2 = freq[j-1], freq[j]
            g1, g2 = mag_db[j-1], mag_db[j]
            if g2 != g1:
                frac = (0 - g1) / (g2 - g1)
            else:
                frac = 0.5
            metrics["ugf"] = f1 + frac * (f2 - f1)
            break

    # Phase Margin (phase at UGF)
    ugf = metrics.get("ugf")
    if ugf:
        for j in range(1, len(freq)):
            if freq[j] >= ugf:
                metrics["phase_margin"] = 180 + phase[j]
                break

    # Gain Margin (gain at phase = -180)
    for j in range(1, len(phase)):
        if phase[j-1] > -180 and phase[j] <= -180:
            metrics["gain_margin"] = -mag_db[j]
            break

    return metrics
def parse_results(
    result: SimulationResult,
    pins: list[PinInfo],
    dut_instance: str = "DUT",
    vdd_value: float = 1.8,
    deck_config: Optional[SimDeckConfig] = None,
    net_aliases: Optional[dict[str, str]] = None,
    classifications: Optional[dict[str, object]] = None,
) -> dict:
    """Extract measurements from Spectre results, organized by pin.

    Detects analysis type (DC/tran/AC) and extracts appropriate metrics.
    For power pins, also computes current and power measurements.
    """
    if not result.ok or not result.data:
        return {"status": "error", "errors": result.errors}

    data = result.data
    analysis = _detect_analysis_type(data)
    pin_measurements = {}
    configured = deck_config.pin_measurements if deck_config is not None else {}
    pins_by_name = {p.name: p for p in pins}
    requested_names = list(configured.keys()) if configured else [p.name for p in pins]
    requested_measured_total = 0
    net_aliases = net_aliases or {}

    for pin_name in requested_names:
        pin = pins_by_name.get(pin_name, PinInfo(pin_name, "", 0.0, 0.0, ""))
        pm = configured.get(pin_name) if configured else None
        measures = set(pm.measures if pm is not None else ["voltage"])
        if not measures:
            continue
        requested_measured_total += 1

        if classifications and pin.name in classifications:
            pad_type = getattr(classifications[pin.name], "pin_type", classify_pin_heuristic(pin))
        else:
            pad_type = classify_pin_heuristic(pin)
        if pad_type == "ground":
            continue

        net_name = net_aliases.get(pin.name, pin.name)
        signal_key = _find_signal(data, net_name, dut_instance)
        if signal_key is None and net_name != pin.name:
            signal_key = _find_signal(data, pin.name, dut_instance)

        if "voltage" in measures:
            # For AC analysis, directly look up "ac:{pin_name}" in data
            ac_key = f"ac:{pin_name}"
            if analysis == "ac" and ac_key in data:
                metrics = _measure_ac(data, pin_name)
            elif signal_key in data:
                sig_vals = data.get(signal_key, [])
                is_single_point = isinstance(sig_vals, list) and len(sig_vals) == 1
                if analysis == "tran":
                    metrics = _measure_tran(data, signal_key)
                elif analysis == "dc":
                    if is_single_point:
                        metrics = _measure_dc_op(data, signal_key)
                    else:
                        metrics = _measure_dc(data, signal_key, vdd=vdd_value)
                else:
                    metrics = _measure_tran(data, signal_key)
            else:
                metrics = {"error": "signal not found in PSF data"}

            metrics["pad_type"] = pad_type
            metrics["psf_key"] = signal_key
            metrics["net"] = net_name

            if "current" in measures or "power" in measures:
                src_name = f"SRC_{pin.name}"
                # Spectre PSF uses various formats for branch currents
                i_candidates = [
                    f"{src_name}:p",        # Spectre short form (PLUS)
                    f"{src_name}:PLUS",     # Full terminal name
                    src_name,               # Total source current
                    f"{src_name}.p",        # Dot notation variant
                ]
                i_key = None
                for cand in i_candidates:
                    i_key = _find_signal(data, cand, dut_instance)
                    if i_key is not None:
                        break

                if i_key and i_key in data:
                    if "current" in measures and analysis == "tran":
                        i_vals = data.get(i_key, [])
                        try:
                            import numpy as np
                            i_arr = np.array(i_vals, dtype=float)
                            if len(i_arr) > 0:
                                metrics["iavg"] = float(np.mean(np.abs(i_arr)))
                                metrics["imax"] = float(np.max(np.abs(i_arr)))
                        except (ImportError, ValueError):
                            pass

                    if "power" in measures:
                        power = _measure_power(data, signal_key, i_key)
                        if "error" not in power:
                            metrics.update(power)
                elif analysis != "tran":
                    # DC OP: single-point current values
                    if "current" in measures:
                        src_name = f"SRC_{pin.name}"
                        i_key = _find_signal(data, f"{src_name}:p", dut_instance)
                        if i_key is None:
                            i_key = _find_signal(data, f"{src_name}:PLUS", dut_instance)
                        if i_key is None:
                            i_key = _find_signal(data, src_name, dut_instance)
                        if i_key and i_key in data:
                            i_vals = data[i_key]
                            i_val = i_vals[0] if isinstance(i_vals, list) and i_vals else i_vals
                            metrics["iavg"] = float(abs(float(i_val))) if i_val is not None else 0
                            metrics["current_avg"] = metrics["iavg"]
                        else:
                            metrics["current_error"] = "current signal not found in PSF data"

            # For AC analysis, also extract DC operating point value from merged data
            if analysis == "ac" and signal_key in data:
                dc_vals = data[signal_key]
                if isinstance(dc_vals, list) and len(dc_vals) == 1:
                    metrics["vdc"] = float(dc_vals[0])
                elif isinstance(dc_vals, (int, float)):
                    metrics["vdc"] = float(dc_vals)

            # For AC analysis, also extract DC current from merged data
            if analysis == "ac" and ("current" in measures or "power" in measures):
                src_name = f"SRC_{pin.name}"
                for cand in [f"{src_name}:p", f"{src_name}:PLUS", src_name, f"{src_name}.p"]:
                    if cand in data:
                        i_vals = data[cand]
                        i_val = i_vals[0] if isinstance(i_vals, list) and i_vals else i_vals
                        if i_val is not None:
                            metrics["iavg"] = float(abs(float(i_val)))
                            metrics["current_avg"] = metrics["iavg"]
                        break
                else:
                    if "current" in measures and "current_avg" not in metrics:
                        metrics["current_error"] = "current signal not found in PSF data"

            pin_measurements[pin.name] = metrics

            pin_measurements[pin.name] = metrics
        else:
            pin_measurements[pin.name] = {
                "pad_type": pad_type,
                "net": net_name,
                "error": "signal not found in PSF data",
            }


    # Extract noise metrics for output pins from noise PSF data
    if data:
        for pin_name, pm in pin_measurements.items():
            if "error" in pm:
                continue
            ptype = pm.get("pad_type", "")
            if ptype not in ("analog_output",):
                continue
            # Look for noise data in merged dict
            noise_keys = [k for k in data.keys() if k.startswith("noise:")]
            if noise_keys:
                import math as _m
                for nk in noise_keys:
                    vals = data[nk]
                    if "total" in nk.lower() and "noise" in nk.lower():
                        if isinstance(vals, list) and len(vals) > 0:
                            val = vals[-1] if isinstance(vals[-1], (int, float)) else float(vals[-1][0]) if isinstance(vals[-1], (list, tuple)) else None
                            if val is not None:
                                pm["noise"] = abs(val)
                    if "density" in nk.lower() or "nkv" in nk.lower():
                        if isinstance(vals, list) and len(vals) > 0:
                            val = vals[0] if isinstance(vals[0], (int, float)) else float(vals[0][0]) if isinstance(vals[0], (list, tuple)) else None
                            if val is not None:
                                pm["noise_density"] = abs(val)

    return {
        "status": "ok",
        "analysis": analysis,
        "num_pins_measured": sum(
            1 for m in pin_measurements.values() if "error" not in m
        ),
        "num_pins_total": requested_measured_total,
        "pins": pin_measurements,
    }


# ── Orchestrator ───────────────────────────────────────────────

@dataclass
class SimRunResult:
    lib: str
    tb_cell: str
    netlist_path: Optional[str]
    deck_path: Optional[str]
    spectre_ok: bool
    spectre: dict = field(default_factory=dict)
    measurements_path: Optional[str] = None
    measurements_summary: dict = field(default_factory=dict)
    plot_paths: list[str] = field(default_factory=list)
    run_dir: Optional[str] = None

    def save(self, run_dir: Path) -> None:
        data = asdict(self)
        data["run_dir"] = str(run_dir)
        (run_dir / "sim_run_result.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def run_sim_run(
    lib: str,
    tb_cell: str,
    pins: list[PinInfo],
    run_dir: Path,
    *,
    config=None,
    deck_config: Optional[SimDeckConfig] = None,
    site: Optional[SiteConfig] = None,
    client: Optional[VirtuosoClient] = None,
    spectre_mode: str = "spectre",
    spectre_timeout: int = 600,
    user_intent: str = "",
    vdd_value: float = 1.8,
) -> SimRunResult:
    """Run the full simulation pipeline (Steps 3a-3d).

    Parameters
    ----------
    lib, tb_cell : Library and testbench cell names
    pins : Pin info list from Step 4b (for result mapping)
    run_dir : Output directory (typically from sim_flow)
    config : SimDeckConfig or None (legacy, used if deck_config is None)
    deck_config : SimDeckConfig (takes priority over config)
    site : SiteConfig (default: loaded from _local/site.yaml)
    client : VirtuosoClient (default: from env)
    spectre_mode : Spectre execution mode
    spectre_timeout : Timeout in seconds
    user_intent : Free-text simulation intent for LLM config generation
    vdd_value : Supply voltage for default config
    """
    if client is None:
        client = VirtuosoClient.from_env()
    if site is None:
        site = SiteConfig.from_env()
    if config is None:
        # Let resolve_sim_config() fall through to sim_config_from_site()
        # which includes both core models (with sections) and IO models.
        config = None

    print(f"\n{'='*60}")
    print(f" Sim Run: {lib}/{tb_cell}")
    print(f" Cds.lib: {site.cds_lib}")
    print(f" IC root: {site.ic_root}")
    print(f"{'='*60}\n")

    # Step 3a: Export netlist
    netlist_path = export_netlist(client, lib, tb_cell, run_dir, site=site)

    # Step 3a.25: Patch netlist with CDF parameters from pin_classifications.json
    # si -batch may not pick up in-memory CDF overrides set via setInstParams.
    # This step directly patches the exported netlist to ensure stimulus values
    # match what was specified in pin_classifications.json.
    if netlist_path is not None:
        _patch_netlist_from_classifications(netlist_path, run_dir, client)

    # Step 3a.5: Write LLM input and resolve sim config
    resolved_config = deck_config
    if netlist_path is not None and resolved_config is None:
        # Write sim_config_input.json for LLM
        try:
            netlist_text = netlist_path.read_text(encoding="utf-8")
            netlist_summary = summarize_netlist(netlist_text)
            pin_classes = None
            pin_class_path = run_dir / "pin_classifications.json"
            if pin_class_path.exists():
                import json as _json
                pin_classes = _json.loads(pin_class_path.read_text(encoding="utf-8")).get("pins", [])
            write_sim_config_input(
                netlist_summary=netlist_summary,
                pin_classifications=pin_classes,
                user_intent=user_intent,
                lib=lib, cell=tb_cell, vdd_value=vdd_value,
                path=run_dir / "sim_config_input.json",
            )
            print(f"[step3a.5] Wrote sim_config_input.json")
        except Exception as e:
            print(f"[step3a.5] WARNING: Failed to write sim config input: {e}")

        # Resolve: LLM > active.state > legacy > site default
        resolved_config = resolve_sim_config(
            run_dir=run_dir,
            lib=lib, cell=tb_cell,
            vdd_value=vdd_value,
            user_intent=user_intent,
            legacy_config=config,
        )

    if resolved_config is None:
        resolved_config = config

    # Append IO pad model include if available in site config
    if site.pdk_io_spectre_include and resolved_config is not None:
        from sim_io.sim.config import ModelInclude
        # Check if IO model is already included
        io_paths = {mi.path for mi in resolved_config.model_includes}
        if site.pdk_io_spectre_include not in io_paths:
            resolved_config.model_includes.append(
                ModelInclude(path=site.pdk_io_spectre_include, section="")
            )
            print(f"[step3b] Added IO model include: {site.pdk_io_spectre_include}")

    # Add current save signals for requested current/power measurements so
    # standalone Spectre deck includes branch currents.
    if resolved_config is not None and isinstance(resolved_config, SimDeckConfig):
        from sim_io.sim.config import SaveSignal
        for pin_name, pm in resolved_config.pin_measurements.items():
            if "current" in pm.measures or "power" in pm.measures:
                src_name = f"SRC_{pin_name}"
                resolved_config.save_signals.append(
                    SaveSignal(signal=f"{src_name}:p")
                )
        if resolved_config.save_signals:
            if resolved_config.save_default == "allpub":
                resolved_config.save_default = "all"

    # Step 3b: Build deck
    deck_path = None
    if netlist_path is not None:
        deck_path = build_deck(netlist_path, resolved_config, run_dir)

    # Step 3c: Run spectre
    spectre_ok = False
    sim_result = None
    if deck_path is not None:
        spectre_cmd = os.getenv("SPECTRE_CMD", "")
        sim_result = run_spectre(
            deck_path, run_dir,
            spectre_cmd=spectre_cmd,
            mode=spectre_mode,
            timeout=spectre_timeout,
            site=site,
            client=client,
        )
        spectre_ok = sim_result.ok

    # Step 3d: Parse results + visualize
    measurements = {}
    plot_paths = []
    if sim_result is not None and sim_result.ok:
        net_aliases = _extract_dut_pin_net_map(deck_path, "DUT") if deck_path else {}
        classifications = None
        pin_class_path = run_dir / "pin_classifications.json"
        if pin_class_path.exists():
            try:
                class_result = load_pin_classifications(pin_class_path)
                classifications = {pc.name: pc for pc in class_result.pins}
            except Exception as e:
                print(f"[step3d] WARNING: Failed to load pin classifications: {e}")
        measurements = parse_results(
            sim_result, pins, vdd_value=vdd_value,
            deck_config=resolved_config if isinstance(resolved_config, SimDeckConfig) else None,
            net_aliases=net_aliases,
            classifications=classifications,
        )
        # Save measurements
        (run_dir / "measurements.json").write_text(
            json.dumps(measurements, indent=2, default=str), encoding="utf-8"
        )

        # Step 3e: Generate SVG plots from PSF results
        try:
            from sim_io.sim.viz import visualize_run, parse_psf_ascii, extract_dc_metrics, extract_ac_metrics

            psf_dir = _resolve_psf_dir(run_dir, deck_path)

            if psf_dir.exists():
                plots_dir = run_dir / "plots"
                plot_signals: list[str] = []
                plot_labels: dict[str, str] = {}
                for pin_name, pin_metrics in (measurements.get("pins") or {}).items():
                    if "error" in pin_metrics:
                        continue
                    psf_key = pin_metrics.get("psf_key")
                    if not psf_key:
                        continue
                    if psf_key not in plot_signals:
                        plot_signals.append(psf_key)
                    # Preserve user-facing pin names in plots even when the
                    # Spectre-visible TB net must be renamed for simulation.
                    plot_labels.setdefault(psf_key, pin_name)

                plot_paths = visualize_run(
                    str(psf_dir),
                    str(plots_dir),
                    signals=plot_signals or None,
                    labels=plot_labels or None,
                )

                # Plots are the persistent visualization artifact. Older runs
                # wrote viz_metrics.json here, but it was often empty for this
                # IO-ring DC/tran route and duplicated measurement extraction.
        except Exception as e:
            print(f"[step3e] Visualization skipped: {e}")

    measurements_summary = {}
    if measurements:
        measurements_summary = {
            "status": measurements.get("status", ""),
            "analysis": measurements.get("analysis", ""),
            "num_pins_measured": measurements.get("num_pins_measured", 0),
            "num_pins_total": measurements.get("num_pins_total", 0),
        }

    result = SimRunResult(
        lib=lib,
        tb_cell=tb_cell,
        netlist_path=str(netlist_path) if netlist_path else None,
        deck_path=str(deck_path) if deck_path else None,
        spectre_ok=spectre_ok,
        spectre=(sim_result.metadata.get("summary", {}) if sim_result is not None else {}),
        measurements_path=str(run_dir / "measurements.json") if measurements else None,
        measurements_summary=measurements_summary,
        plot_paths=[str(p) for p in plot_paths],
    )
    result.save(run_dir)

    print(f"\n{'='*60}")
    print(f" Sim Run Summary")
    print(f"{'='*60}")
    print(f"  Netlist:  {'OK' if netlist_path else 'FAILED'}")
    print(f"  Deck:     {'OK' if deck_path else 'FAILED'}")
    print(f"  Spectre:  {'OK' if spectre_ok else 'FAILED'}")
    if measurements.get("pins"):
        ok_pins = sum(1 for m in measurements["pins"].values() if "error" not in m)
        print(f"  Measured: {ok_pins}/{measurements['num_pins_total']} pins")
    if plot_paths:
        print(f"  Plots:    {len(plot_paths)} SVG files in {run_dir / 'plots'}")
    print(f"{'='*60}\n")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: python sim_run.py <lib> <tb_cell> [model_include]")
        print(f"Example: python sim_run.py LLM_Layout_Design_Lab IO_RING_12x12_tb")
        sys.exit(1)

    _lib = sys.argv[1]
    _tb_cell = sys.argv[2]
    _model = sys.argv[3] if len(sys.argv) > 3 else ""

    _run_dir = create_run_dir()

    _site = SiteConfig.from_env()
    from sim_io.sim.config import sim_config_from_site
    _cfg = sim_config_from_site(vdd_value=1.8)
    if _model or _site.pdk_spectre_include:
        from sim_io.sim.config import ModelInclude
        _cfg.model_includes.insert(0, ModelInclude(
            path=_model or _site.pdk_spectre_include, section="TT"
        ))
    run_sim_run(_lib, _tb_cell, [], _run_dir, deck_config=_cfg, site=_site)

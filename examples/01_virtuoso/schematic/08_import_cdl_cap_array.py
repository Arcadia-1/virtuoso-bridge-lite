#!/usr/bin/env python3
"""Import a 4-bit binary-weighted capacitor array into Virtuoso via spiceIn.

Generates a CDL netlist locally, uploads it, and runs Cadence spiceIn
over SSH to create the schematic automatically (with wiring).

Circuit:
    cap_unit:     single 10fF capacitor (TOP, BOT)
    cap_array_4b: 15 unit caps, weights [1,2,4,8], pins TOP + BOT<3:0>

Prerequisites:
- virtuoso-bridge tunnel running
- spiceIn on remote (auto-detected from Cadence install)

Note:
    spiceIn must run via SSH, never via SKILL system() — it starts an
    internal Virtuoso process and will deadlock the CIW.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.transport.remote_paths import (
    default_virtuoso_bridge_dir,
    resolve_remote_username,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LIB = "PLAYGROUND_LLM"

CDL = """\
.SUBCKT cap_unit TOP BOT
C0 TOP BOT cap C=1.0000e-14
.ENDS

.SUBCKT cap_array_4b TOP BOT<3:0>
XC_b0_0 TOP BOT<0> cap_unit
XC_b1_0 TOP BOT<1> cap_unit
XC_b1_1 TOP BOT<1> cap_unit
XC_b2_0 TOP BOT<2> cap_unit
XC_b2_1 TOP BOT<2> cap_unit
XC_b2_2 TOP BOT<2> cap_unit
XC_b2_3 TOP BOT<2> cap_unit
XC_b3_0 TOP BOT<3> cap_unit
XC_b3_1 TOP BOT<3> cap_unit
XC_b3_2 TOP BOT<3> cap_unit
XC_b3_3 TOP BOT<3> cap_unit
XC_b3_4 TOP BOT<3> cap_unit
XC_b3_5 TOP BOT<3> cap_unit
XC_b3_6 TOP BOT<3> cap_unit
XC_b3_7 TOP BOT<3> cap_unit
.ENDS
"""

DEVMAP = """\
devselect := resistor res
devselect := capacitor cap
devselect := inductor ind
"""


# ---------------------------------------------------------------------------
# SSH command builder (for running spiceIn over SSH)
# ---------------------------------------------------------------------------

def _ssh_cmd(client: VirtuosoClient) -> list[str]:
    t = client._tunnel
    h, u = t._remote_host, t._remote_user
    jh, ju = getattr(t, "_jump_host", None), getattr(t, "_jump_user", None)
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=30"]
    if jh:
        cmd += ["-J", f"{ju}@{jh}"]
    cmd.append(f"{u}@{h}")
    return cmd


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    client = VirtuosoClient.from_env()

    # Discover paths from running Virtuoso
    env = {}
    for var in ["LM_LICENSE_FILE", "CDS_LIC_FILE", "LD_LIBRARY_PATH"]:
        r = client.execute_skill(f'getShellEnvVar("{var}")')
        val = r.output.strip('"') if r.output and r.output.strip() != "nil" else ""
        if val:
            env[var] = val

    r = client.execute_skill('cdsGetInstPath()')
    cds_inst = r.output.strip('"')
    spicein = f"{cds_inst}/bin/spiceIn"

    r = client.execute_skill(f'ddGetObj("{LIB}")~>writePath')
    work_dir = str(Path(r.output.strip('"')).parent)

    username = resolve_remote_username(
        configured_user=client._tunnel._remote_user,
        runner=client._tunnel._ssh_runner,
    )
    remote_tmp = default_virtuoso_bridge_dir(username, "cap_array")

    # 1. Write CDL and devmap locally, upload via bridge
    cdl_path = f"{remote_tmp}/cap_array_4b.cdl"
    devmap_path = f"{remote_tmp}/devmap.txt"

    client._tunnel.upload_text(CDL, cdl_path)
    client._tunnel.upload_text(DEVMAP, devmap_path)
    print(f"[cdl]\n{CDL}")
    print("[upload] Done")

    # 2. Run spiceIn via SSH (not via SKILL — would deadlock CIW)
    script = f"""#!/bin/bash
export HOSTNAME=$(hostname 2>/dev/null || echo localhost)
export LM_LICENSE_FILE="{env.get('LM_LICENSE_FILE', '')}"
export CDS_LIC_FILE="{env.get('CDS_LIC_FILE', '')}"
export CDS_INST_DIR={cds_inst}
export IC_HOME=$CDS_INST_DIR
export PATH=$CDS_INST_DIR/bin:$CDS_INST_DIR/tools/bin:$PATH
export LD_LIBRARY_PATH="{env.get('LD_LIBRARY_PATH', '')}"
cd {work_dir}
{spicein} -language SPICE -netlistFile {cdl_path} \\
  -outputLib {LIB} -reflibList "analogLib basic" \\
  -devmapFile {devmap_path}
"""
    script_path = f"{remote_tmp}/run.sh"
    client._tunnel.upload_text(script, script_path)

    print("[spiceIn] Running...")
    result = subprocess.run(
        _ssh_cmd(client) + [f"bash {script_path}"],
        capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr
    if "successfully imported" not in output:
        print(output)
        print("[spiceIn] FAILED")
        return 1
    print("[spiceIn] OK")

    # 3. Generate symbol for cap_unit
    client.execute_skill("ddUpdateLibList()")
    client.execute_skill(f'''
    schPinListToSymbol("{LIB}" "cap_unit" "symbol"
      schSchemToPinList("{LIB}" "cap_unit" "schematic"))
    ''')
    r = client.execute_skill(f'ddGetObj("{LIB}" "cap_unit")~>views~>name')
    print(f"[symbol] cap_unit: {r.output}")

    # 4. Open
    r = client.execute_skill(f'ddGetObj("{LIB}" "cap_array_4b")~>views~>name')
    print(f"[verify] cap_array_4b: {r.output}")
    client.open_window(LIB, "cap_array_4b", view="schematic")
    print("[done] Opened cap_array_4b")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

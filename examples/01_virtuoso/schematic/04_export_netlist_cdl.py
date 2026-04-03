#!/usr/bin/env python3
"""Export a schematic as an auCdl netlist and print the real netlist text.

This example avoids driving the Virtuoso UI forms directly. Instead it:
  1. writes a minimal ``si.env`` on the remote Virtuoso host
  2. runs ``si -batch -command netlist``
  3. reads the generated ``.src.net`` file back through SKILL

Usage::

    python 17_export_schematic_cdl.py
    python 17_export_schematic_cdl.py PLAYGROUND_LLM LB_SAR_BTS
    python 17_export_schematic_cdl.py PLAYGROUND_LLM LB_SAR_BTS T28
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "file_ops.il"


def _si_env_text(lib: str, cell: str, tech_node: str, run_dir: str) -> str:
    inc_var = "incFILE_180" if tech_node.upper().startswith("T180") or tech_node.startswith("180") else "incFILE_28"
    return "\n".join([
        'simStopList = \'("auCdl")',
        'simViewList = \'("auCdl" "schematic")',
        'auCdlDefNetlistProc = "ansCdlSubcktCall"',
        'globalGndSig = ""',
        'globalPowerSig = ""',
        "shrinkFACTOR = 0",
        'checkScale = "meter"',
        "preserveDIO = 'nil", "checkDIOAREA = 'nil", "checkDIOPERI = 'nil",
        "preserveCAP = 'nil", "checkCAPVAL = 'nil", "checkCAPAREA = 'nil", "checkCAPPERI = 'nil",
        "preserveRES = 'nil", "checkRESVAL = 'nil", "checkRESSIZE ='nil",
        'resistorModel = ""', "shortRES = 2000",
        "simNetlistHier = 't", "pinMAP = 'nil", "displayPININFO = 't", "checkLDD = 'nil",
        'connects = ""', 'setEQUIV = ""',
        f'simRunDir = "{run_dir}"',
        f'hnlNetlistFileName = "{cell}.src.net"',
        'simSimulator = "auCdl"', 'simViewName = "schematic"',
        f'simCellName = "{cell}"', f'simLibName = "{lib}"',
        f'incFILE = ""',
        'cdlSimViewList = \'("auCdl" "schematic")',
        'cdlSimStopList = \'("auCdl")',
        "",
    ])


def _netlist_shell_command(run_dir: str, cds_lib: str | None) -> str:
    log = f"{run_dir}/si.log"
    cdslib_arg = f"-cdslib {shlex.quote(cds_lib)}" if cds_lib else ""
    return f"cd {shlex.quote(run_dir)} ; si -batch -command netlist {cdslib_arg} >& {log}"


def main() -> int:
    lib  = sys.argv[1] if len(sys.argv) > 1 else None
    cell = sys.argv[2] if len(sys.argv) > 2 else None
    tech = sys.argv[3] if len(sys.argv) > 3 else "T28"

    client = VirtuosoClient.from_env()

    if not lib or not cell:
        lib, cell, view = client.get_current_design(timeout=10)
        if not lib or not cell or view != "schematic":
            print("No active schematic. Run 01a_create_rc_stepwise.py first, then rerun this.")
            return 1

    run_dir = f"/tmp/virtuoso_bridge_netlist_export/{lib}/{cell}"
    env_path = f"{run_dir}/si.env"
    netlist_path = f"{run_dir}/{cell}.src.net"

    print(f"Exporting auCdl: {lib}/{cell}/schematic  [{tech}]")
    load_elapsed, load_result = timed_call(lambda: client.load_il(IL_FILE, timeout=20))
    meta = load_result.metadata
    print(f"[load_il] {'uploaded' if meta.get('uploaded') else 'cache hit'}  [{format_elapsed(load_elapsed)}]")

    def skill(cmd: str) -> str:
        return decode_skill(client.execute_skill(cmd, timeout=30).output or "")

    e = escape_skill_string

    # Derive cds.lib path from the library's location on the remote host
    lib_read_path = decode_skill(
        client.execute_skill(f'ddGetObj("{lib}")~>readPath', timeout=10).output or ""
    )
    cds_lib = f"{'/'.join(lib_read_path.rstrip('/').split('/')[:-1])}/cds.lib" if lib_read_path else None

    client.run_shell_command(f"mkdir -p {shlex.quote(run_dir)}", timeout=10)
    skill(f'VbWriteFile("{e(env_path)}" "{e(_si_env_text(lib, cell, tech, run_dir))}")')

    log_path = f"{run_dir}/si.log"
    shell_elapsed, shell_resp = timed_call(
        lambda: client.run_shell_command(_netlist_shell_command(run_dir, cds_lib), timeout=120)
    )
    print(f"[si -batch] [{format_elapsed(shell_elapsed)}]")

    log = skill(f'VbReadFile("{e(log_path)}")')
    if log:
        print(f"[si log]\n{log}")

    netlist = skill(f'VbReadFile("{e(netlist_path)}")')
    print(f"\nNetlist: {netlist_path}\n")
    print(netlist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

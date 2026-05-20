#!/usr/bin/env python3
"""digital_import — one-shot Virtuoso import of a PnR digital block.

Composes the 4-step pipeline (strmin → ihdl → PG labels → restyle) that
was previously manual.  Lab-specific defaults are hardcoded so the daily
invocation is short:

  python digital_import.py --top FIFO_4096x24b --target-lib DIG_OUTPUT_TEST3 \\
         --sram-cell TS1N28HPCPSVTB4096X24M8S

What it does automatically:
  • Auto-detects the SRAM cell name from the Verilog netlist (grep for
    TS1N28*); only one unique cell allowed.  Pass --sram-cell to override
    or to handle multi-bank designs.
  • Pre-imports the SRAM into the `sram` lib (if cell isn't already there)
  • Derives GDS/Verilog paths from <top> (under /home/zhangz/.../DIG_SYN_AI/)
  • Derives SRAM GDS path from <sram_cell>
  • Uses ihdl power/ground nets VDDD/VSSS (no collision with RTL port names)
  • Uses tech-lib tsmcN28 and stdcell ref tcbn28hpcplusbwp12t30p140
  • Skips SRAM pre-import if the cell already exists in `sram` lib

Lab paths default to thu-wei's standard locations.  Override per-user
via env vars without editing this file:

  VB_DIG_SYN_ROOT  - directory containing <top>/apr/PNR_SIGNOFF/RESULTS/...
  VB_SRAM_ROOT     - directory containing <sram_cell>_180a/GDSII/...

PDK lib names (TECH_LIB, STDCELL_LIB) and ihdl power/ground net names
are stable across users — edit them in place if your PDK differs.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from virtuoso_bridge.virtuoso.ops import q as _q

SRAM_CELL_REGEX = re.compile(r"\bTS1N28[A-Z0-9]+\b")

SCRIPT_DIR = Path(__file__).resolve().parent
TMP_DIR = Path(tempfile.gettempdir())

# Lab-fixed defaults — override the two ROOTs via env vars so different
# users / lab setups don't have to fork this file.  The PDK lib names
# (TECH_LIB, STDCELL_LIB) and ihdl net names (POWER_NET, GROUND_NET) are
# stable across users — edit in place if your PDK differs.
TECH_LIB     = "tsmcN28"
STDCELL_LIB  = "tcbn28hpcplusbwp12t30p140"
SRAM_LIB     = "sram"
POWER_NET    = "VDDD"   # ihdl iron rule: must NOT collide with RTL VDD/VSS
GROUND_NET   = "VSSS"
DIG_SYN_ROOT = os.environ.get("VB_DIG_SYN_ROOT", "/home/zhangz/TSMC28N/DIG_SYN_AI")
SRAM_ROOT    = os.environ.get("VB_SRAM_ROOT",    "/home/zhangz/TSMC28N/SRAM/tsn28hpcpd127spsram_20120200_180a")


def run(name: str, argv: list[str]) -> None:
    print(f"\n=== {name} ===", flush=True)
    t0 = time.time()
    rc = subprocess.run([sys.executable, *argv], cwd=SCRIPT_DIR).returncode
    dt = time.time() - t0
    if rc != 0:
        print(f"[digital-import] {name} FAILED (rc={rc}, {dt:.0f}s)", file=sys.stderr)
        sys.exit(rc)
    print(f"[digital-import] {name} done ({dt:.0f}s)")


def sram_exists(client, lib: str, cell: str) -> bool:
    r = client.execute_skill(f'when(ddGetObj("{lib}" "{cell}") "exists")')
    return "exists" in (r.output or "")


def detect_sram_cells(client, verilog_path: str) -> list[str]:
    """Return the unique sorted list of TS1N28* cell names referenced by
    *verilog_path*.

    Reads the file locally when the path exists on the caller's filesystem
    (handles user-supplied --verilog pointing at a Windows path); otherwise
    greps the file on the remote Virtuoso host via SKILL ``system()`` and
    reads the grep output back through SKILL ``infile``.
    """
    p = Path(verilog_path)
    if p.exists():
        return sorted(set(SRAM_CELL_REGEX.findall(p.read_text(errors="replace"))))

    remote_tmp = f"/tmp/vb_sram_detect_{os.getpid()}.out"
    cmd = (
        f"grep -hoE 'TS1N28[A-Z0-9]+' {shlex.quote(verilog_path)} "
        f"| sort -u > {shlex.quote(remote_tmp)}"
    )
    client.execute_skill(f"system({_q(cmd)})")

    read_skill = (
        f"let((p line body) "
        f"  p = infile({_q(remote_tmp)}) "
        f"  body = \"\" "
        f"  when(p "
        f"    while(gets(line p) body = strcat(body line)) "
        f"    close(p)) "
        f"  body)"
    )
    r = client.execute_skill(read_skill)
    body = (r.output or "").strip()
    if body.startswith('"') and body.endswith('"'):
        body = body[1:-1]
    body = body.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
    return sorted({
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith("TS1N28")
    })


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", required=True,
                    help="Top cell name (e.g. FIFO_4096x24b).  Drives path auto-detect.")
    ap.add_argument("--target-lib", required=True,
                    help="Target Virtuoso lib for the design (e.g. DIG_OUTPUT_TEST3)")
    ap.add_argument("--sram-cell", default=None,
                    help="SRAM cell name (e.g. TS1N28HPCPSVTB4096X24M8S).  When "
                         "omitted, the Verilog is scanned for TS1N28* references; "
                         "unique match is used automatically, multi-bank designs "
                         "must pass this flag explicitly, and plain-digital "
                         "designs (no TS1N28* in the Verilog) skip SRAM steps.")
    ap.add_argument("--gds", default=None,
                    help="Override design GDS path (default: PNR_SIGNOFF/RESULTS/<top>.route_tapeout.gds)")
    ap.add_argument("--verilog", default=None,
                    help="Override design Verilog path (default: same dir, .ipg_import_elc.v)")
    args = ap.parse_args()

    pnr_dir = f"{DIG_SYN_ROOT}/{args.top}/apr/PNR_SIGNOFF/RESULTS"
    if args.gds is None:
        args.gds = f"{pnr_dir}/{args.top}.route_tapeout.gds"
    if args.verilog is None:
        args.verilog = f"{pnr_dir}/{args.top}.ipg_import_elc.v"

    # Auto-detect SRAM cell from the Verilog when not specified.  The same
    # client is reused for the SRAM pre-import step below.
    client = None
    if args.sram_cell is None:
        from virtuoso_bridge import VirtuosoClient
        client = VirtuosoClient.from_env()
        detected = detect_sram_cells(client, args.verilog)
        if len(detected) == 1:
            args.sram_cell = detected[0]
            print(f"[digital-import] auto-detected SRAM cell: {args.sram_cell}")
        elif len(detected) > 1:
            sys.exit(
                f"ERROR: Verilog references multiple SRAM cells {detected}; "
                f"pass --sram-cell <name> to pick one."
            )
        else:
            print("[digital-import] no TS1N28* in Verilog — plain-digital flow")

    # ref-libs: file format (one per line) for strmin, comma-separated for ihdl.
    ref_libs = [STDCELL_LIB]
    if args.sram_cell:
        ref_libs.append(SRAM_LIB)
    ihdl_ref_libs = ",".join(ref_libs)
    strmin_ref_file = TMP_DIR / "_digital_import_reflibs.txt"
    strmin_ref_file.write_text("\n".join(ref_libs) + "\n")

    # Pre-import SRAM if needed.
    if args.sram_cell:
        if client is None:
            from virtuoso_bridge import VirtuosoClient
            client = VirtuosoClient.from_env()
        if sram_exists(client, SRAM_LIB, args.sram_cell):
            print(f"[digital-import] SRAM {args.sram_cell} already in {SRAM_LIB}, skipping pre-import")
        else:
            stem = args.sram_cell.lower()
            sram_gds = f"{SRAM_ROOT}/{stem}_180a/GDSII/{stem}_180a.gds"
            empty_ref = TMP_DIR / "_digital_import_empty_ref.txt"
            empty_ref.write_text("")
            run("strmin SRAM macro", [
                "import_gds.py", sram_gds,
                "--target-lib", SRAM_LIB, "--tech-lib", TECH_LIB,
                "--ref-libs", str(empty_ref), "--cell", args.sram_cell,
            ])

    run("strmin design GDS", [
        "import_gds.py", args.gds,
        "--target-lib", args.target_lib, "--tech-lib", TECH_LIB,
        "--ref-libs", str(strmin_ref_file),
    ])

    run("ihdl design Verilog", [
        "import_verilog.py", args.verilog,
        "--target-lib", args.target_lib, "--ref-libs", ihdl_ref_libs,
        "--power-net", POWER_NET, "--ground-net", GROUND_NET,
    ])

    run("add power labels", [
        "add_power_labels.py", "--target-lib", args.target_lib, "--cell", args.top,
    ])

    run("restyle labels", [
        "restyle_labels.py", "--target-lib", args.target_lib, "--cell", args.top,
    ])

    print(f"\n[digital-import] {args.target_lib}/{args.top} fully imported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Import a structural Verilog netlist as schematic + symbol + functional via ``ihdl``.

Uses Cadence's standalone ``ihdl`` batch tool (the command-line entry
point documented for "Import Verilog" in the Virtuoso Design Environment
User Guide).  No GUI involvement, no form bootstrap — ihdl runs through
SKILL ``system()`` so it inherits Virtuoso's PATH and licence env.

Prerequisites
-------------
* ``virtuoso-bridge start`` is running, daemon loaded in CIW.
* The target library is already DEFINEd in the Virtuoso work directory's
  ``cds.lib``.
* All reference libraries that supply leaf-cell *symbols* (e.g. the
  standard-cell library) are also DEFINEd.

The importer expects a *structural* netlist — the kind Innovus emits as
``<design>_import.v``.  Behavioural always-blocks won't elaborate to the
cell library.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import uuid

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import q as _q


# Cadence batch parameter file (key := value).  Field semantics taken from
# the official VerilogIn testcase distribution.
#
# structural_views encoded values (verified empirically against the GUI):
#   1 = schematic only
#   5 = schematic + functional      ← testcase default
#
# import_lib_cells := 1  matches GUI "Verilog Cell Modules = Import"
# import_lib_cells := 0  matches GUI "Verilog Cell Modules = Create Symbol Only"
PARAM_TEMPLATE = """\
dest_sch_lib := {target_lib}
ref_lib_list := {ref_libs}
import_if_exists := 1
import_cells := 0
import_lib_cells := {import_lib_cells}
structural_views := {structural_views}
schematic_view_name := {schematic_view}
functional_view_name := functional
netlist_view_name := netlist
symbol_view_name := {symbol_view}
log_file_name := ./verilogIn.batch.log
map_file_name := ./verilogIn.batch.map.table
work_area := ./
power_net := {power_net}
ground_net := {ground_net}
"""

STRUCTURAL_VIEWS = {
    "schematic":                 1,
    "schematic_and_functional":  5,
}


def _skill_write_file(path: str, content: str) -> str:
    """Build a SKILL snippet that writes *content* to *path* line by line.

    Writing one line per ``fprintf`` keeps SKILL string literals on a
    single line each — robust to multi-line content without requiring
    backslash-escape gymnastics.
    """
    parts = [f"let((p) p = outfile({_q(path)} \"w\")"]
    for line in content.splitlines():
        parts.append(f'fprintf(p "%s\\n" {_q(line)})')
    parts.append("close(p))")
    return " ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 2)[0])
    parser.add_argument(
        "verilog",
        help="Path to the structural Verilog file (e.g. LFSR_32BIT_import.v)",
    )
    parser.add_argument(
        "--target-lib", required=True,
        help="OA library to write schematic+symbol+functional into",
    )
    parser.add_argument(
        "--ref-libs", default="tcbn28hpcplusbwp12t30p140",
        help="Comma-separated reference libraries supplying leaf-cell symbols "
             "(default: tcbn28hpcplusbwp12t30p140)",
    )
    parser.add_argument(
        "--schematic-view", default="schematic",
        help="Schematic view name (default: schematic)",
    )
    parser.add_argument(
        "--symbol-view", default="symbol",
        help="Symbol view name (default: symbol)",
    )
    parser.add_argument(
        "--power-net", default="VDDD",
        help="Power-net name in the schematic (default: VDDD)",
    )
    parser.add_argument(
        "--ground-net", default="VSSS",
        help="Ground-net name in the schematic (default: VSSS)",
    )
    parser.add_argument(
        "--structural-views", default="schematic",
        choices=sorted(STRUCTURAL_VIEWS.keys()),
        help="What views to create for structural modules "
             "(default: schematic; pass schematic_and_functional for both)",
    )
    parser.add_argument(
        "--import-lib-cells", action="store_true",
        help="Also create cellviews for leaf modules (GUI 'Verilog Cell Modules = Import'); "
             "default is to leave them as references to the --ref-libs symbols",
    )
    parser.add_argument(
        "--cell", default=None,
        help="Override the top cell to verify after import "
             "(default: Verilog filename stem with any '_import' suffix removed)",
    )
    args = parser.parse_args()

    client = VirtuosoClient.from_env()

    # 1. Target library must be registered in Virtuoso's cds.lib.
    r = client.execute_skill(
        f'sprintf(nil "%L" ddGetObj({_q(args.target_lib)})~>readPath)'
    )
    if (r.output or "").strip() in ('"nil"', "nil", ""):
        sys.exit(
            f"ERROR: library '{args.target_lib}' is not in Virtuoso's cds.lib.\n"
            f"  Add a 'DEFINE {args.target_lib} <path>' line first."
        )

    # 2. Discover Virtuoso's working dir (cds.lib + ihdl run there).
    #    getWorkingDir() returns a SKILL string; the bridge wraps it in one
    #    layer of double-quotes ("..."), and inside that the SKILL %L escape
    #    can re-introduce \" if we sprintf-quote it.  Calling getWorkingDir()
    #    directly avoids the second layer; strip() then handles the single
    #    outer pair cleanly.
    r = client.execute_skill('getWorkingDir()')
    workdir = (r.output or "").strip()
    if workdir.startswith('"') and workdir.endswith('"'):
        workdir = workdir[1:-1]
    # final unescape — defense in depth against any further backslash quoting
    workdir = workdir.replace('\\"', '"').replace('\\\\', '\\')
    if not workdir:
        sys.exit("ERROR: could not determine Virtuoso working directory")

    # 2b. ihdl runs in Virtuoso's workdir and resolves the verilog path
    #     relative to that cwd — NOT to the caller's pwd.  If the user
    #     pointed at a local file, upload it to workdir and rewrite the
    #     arg to the basename.  Without this, ihdl will hit OPEN_FAILED /
    #     no-such-file and the symptom is buried in verilogIn.batch.log.
    from pathlib import Path as _Path
    _vp = _Path(args.verilog)
    if _vp.exists():
        _remote = f"{workdir}/{_vp.name}"
        print(f"[stage] verilog: uploading {_vp} -> {_remote}")
        _rr = client.upload_file(str(_vp), _remote)
        if getattr(_rr, "status", None) is not None and str(_rr.status).endswith("ERROR"):
            sys.exit(f"ERROR: failed to upload verilog {_vp} to {_remote}: "
                     f"{getattr(_rr, 'errors', None)}")
        args.verilog = _vp.name

    # 3. Compose ihdl_parameter content.
    param_content = PARAM_TEMPLATE.format(
        target_lib=args.target_lib,
        ref_libs=args.ref_libs,
        schematic_view=args.schematic_view,
        symbol_view=args.symbol_view,
        power_net=args.power_net,
        ground_net=args.ground_net,
        structural_views=STRUCTURAL_VIEWS[args.structural_views],
        import_lib_cells=1 if args.import_lib_cells else 0,
    )

    # 4. Write the two config files on the remote host via SKILL outfile.
    tag = uuid.uuid4().hex[:8]
    param_path = f"/tmp/vb_ihdl_param_{tag}"
    files_path = f"/tmp/vb_ihdl_files_{tag}"
    r = client.execute_skill(_skill_write_file(param_path, param_content))
    if r.errors:
        sys.exit(f"failed to write {param_path}: {r.errors}")
    r = client.execute_skill(_skill_write_file(files_path, f"-param {param_path}\n"))
    if r.errors:
        sys.exit(f"failed to write {files_path}: {r.errors}")

    # 5. Run ihdl through SKILL system() — Virtuoso already has IC PATH set.
    cdslib = f"{workdir}/cds.lib"
    cmd = (
        f"cd {shlex.quote(workdir)} && "
        f"ihdl -cdslib {shlex.quote(cdslib)} "
        f"-f {shlex.quote(files_path)} "
        f"{shlex.quote(args.verilog)}"
    )
    print(f"[ihdl] {cmd}")
    r = client.execute_skill(f"system({_q(cmd)})")
    rc_text = (r.output or "").strip()
    try:
        rc = int(rc_text)
    except ValueError:
        rc = -1
    if rc != 0:
        sys.exit(
            f"ihdl failed (system() returned {rc_text!r}).\n"
            f"  See {workdir}/verilogIn.batch.log for details."
        )

    # 6. Refresh Virtuoso's library cache so Library Manager sees the new cell.
    client.execute_skill("ddUpdateLibList()")

    # 7. Verify imported views + schematic content.
    cell = args.cell or os.path.basename(args.verilog).rsplit(".", 1)[0]
    if cell.endswith("_import"):
        cell = cell[: -len("_import")]

    r = client.execute_skill(
        f'sprintf(nil "%L" '
        f'  mapcar(lambda(v v~>name) ddGetObj({_q(args.target_lib)} {_q(cell)})~>views))'
    )
    print(f"[OK] {args.target_lib}/{cell}/views: {r.output}")

    sk_sch = (
        f"let((cv) "
        f"  cv=dbOpenCellViewByType({_q(args.target_lib)} {_q(cell)} {_q(args.schematic_view)} nil \"r\") "
        f"  if(cv "
        f"     sprintf(nil \"instances=%d nets=%d terms=%d\" "
        f"             length(cv~>instances) length(cv~>nets) length(cv~>terminals)) "
        f"     \"OPEN_FAILED\")) "
    )
    r = client.execute_skill(sk_sch)
    print(f"[OK] {args.target_lib}/{cell}/{args.schematic_view}: {r.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

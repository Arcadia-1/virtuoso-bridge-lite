#!/usr/bin/env python3
"""Import a routed GDS into a Virtuoso library via Cadence ``strmin``.

The standalone ``strmin`` tool is invoked through SKILL ``system()`` —
strmin inherits the running Virtuoso's PATH, licence env, and working
directory, so no SSH or local-shell setup is needed.

Prerequisites
-------------
* ``virtuoso-bridge start`` is running, daemon loaded in CIW.
* The target library is already DEFINEd in the Virtuoso work dir's
  ``cds.lib``.  ``strmin`` creates the cellview directories but does
  not amend ``cds.lib``.

Reference libraries
-------------------
* ``--ref-libs <file>`` (recommended) — plain text file listing the
  referenced lib names, one per line.  Lab convention is
  ``<workdir>/ref``.  Keeps import scope explicit and auditable.
* ``--use-cds-lib`` — shortcut for strmin's magic ``-refLibList
  XST_CDS_LIB``: refs **every** lib in the work dir's cds.lib
  (including ``INCLUDE`` chains).  Unsafe unless the cds.lib is
  strictly curated — same-name cells across PDK / IP / historical
  libs will silently bind to the wrong one.

The script prints instance/shape counts of the new layout cellview as a
sanity check after import.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string


def _q(s: str) -> str:
    """Wrap a Python string as a SKILL string literal."""
    return f'"{escape_skill_string(s)}"'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 2)[0])
    parser.add_argument(
        "gds",
        help="Path to the .gds file (must be readable by the Virtuoso process)",
    )
    parser.add_argument(
        "--target-lib", required=True,
        help="OA library to write into (must already be DEFINEd in cds.lib)",
    )
    parser.add_argument(
        "--tech-lib", default="tsmcN28",
        help="OA library that supplies the tech file (default: tsmcN28)",
    )
    refgrp = parser.add_mutually_exclusive_group()
    refgrp.add_argument(
        "--ref-libs", default=None,
        help="(recommended) File path passed to strmin -refLibList — a "
             "plain text file with one referenced lib name per line "
             "(e.g. tcbn28hpcplus..., tphn28hpcpgv18...).  Mutually "
             "exclusive with --use-cds-lib.",
    )
    refgrp.add_argument(
        "--use-cds-lib", action="store_true",
        help="UNSAFE shortcut for `-refLibList XST_CDS_LIB`: refs every "
             "lib in the work dir's cds.lib (incl. INCLUDE chains).  "
             "Risk: same-name cells across PDK / IP / old project libs "
             "will silently bind to the wrong one.  Use only with a "
             "strictly curated cds.lib; prefer --ref-libs.  Mutually "
             "exclusive with --ref-libs.",
    )
    parser.add_argument(
        "--cell", default=None,
        help="Override the cell name to verify after import "
             "(default: stem of the GDS file, splitting on '.')",
    )
    args = parser.parse_args()

    client = VirtuosoClient.from_env()

    # 1. Make sure the target library is registered in cds.lib.
    r = client.execute_skill(
        f'sprintf(nil "%L" ddGetObj({_q(args.target_lib)})~>readPath)'
    )
    if (r.output or "").strip() in ('"nil"', "nil", ""):
        sys.exit(
            f"ERROR: library '{args.target_lib}' is not in Virtuoso's cds.lib.\n"
            f"  Add a 'DEFINE {args.target_lib} <path>' line and restart Virtuoso, "
            f"or call ddUpdateLibList() first."
        )

    # 2. Compose the strmin command line.  Use shlex.quote so paths with
    #    spaces or odd chars survive the trip through SKILL's system().
    parts = [
        "strmin",
        "-library",            shlex.quote(args.target_lib),
        "-strmFile",           shlex.quote(args.gds),
        "-attachTechFileOfLib", shlex.quote(args.tech_lib),
        "-logFile",            "strmIn.log",
    ]
    if args.use_cds_lib:
        # XST_CDS_LIB is a magic literal that strmin understands as
        # "use every lib defined in the cds.lib resolved from cwd".
        # Not a path — must NOT be shell-quoted as a filename.
        parts += ["-refLibList", "XST_CDS_LIB"]
    elif args.ref_libs:
        parts += ["-refLibList", shlex.quote(args.ref_libs)]
    parts.append("-replaceBusBitChar")
    cmd = " ".join(parts)

    print(f"[strmin] {cmd}")
    r = client.execute_skill(f"system({_q(cmd)})")
    rc_text = (r.output or "").strip()
    try:
        rc = int(rc_text)
    except ValueError:
        rc = -1
    if rc != 0:
        sys.exit(
            f"strmin failed (system() returned {rc_text!r}).  "
            f"Look at strmIn.log in Virtuoso's working directory for details."
        )

    # 3. Refresh Virtuoso's library cache so Library Manager sees the cells.
    client.execute_skill("ddUpdateLibList()")

    # 4. Verify by counting layout objects in the imported cell.
    cell = args.cell or Path(args.gds).name.split(".")[0]
    skill = (
        f"let((cv) "
        f"  cv=dbOpenCellViewByType({_q(args.target_lib)} {_q(cell)} \"layout\" nil \"r\") "
        f"  if(cv "
        f"     sprintf(nil \"instances=%d shapes=%d bbox=%L\" "
        f"             length(cv~>instances) length(cv~>shapes) cv~>bBox) "
        f"     \"OPEN_FAILED — different cell name? See strmIn.log.\")) "
    )
    r = client.execute_skill(skill)
    print(f"[OK] {args.target_lib}/{cell}/layout: {r.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

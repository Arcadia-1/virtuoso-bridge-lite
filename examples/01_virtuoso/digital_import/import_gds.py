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
import time
from pathlib import Path

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import q as _q


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

    # MSYS / Git Bash path-mangling check.  Git Bash on Windows
    # rewrites Linux-style /home/... into <msys_root>/home/... before
    # argv reaches Python — strmin then can't find the file and the
    # error message points at the *mangled* path, which is confusing.
    # Detect and bail with a clear pointer to the fix.
    if args.gds.startswith(("C:/", "C:\\", "D:/", "D:\\")) and "/home/" in args.gds.replace("\\", "/"):
        unmangled = "/home/" + args.gds.replace("\\", "/").split("/home/", 1)[1]
        sys.exit(
            "ERROR: GDS path appears mangled by Git Bash / MSYS / Cygwin.\n"
            f"  Received:  {args.gds}\n"
            f"  Expected:  {unmangled}\n"
            "  Cause:     these shells translate Linux paths like /home/... to "
            "<msys_root>/home/... before Python sees the argv.\n"
            "  Fix:       on Windows, run this script from PowerShell, cmd.exe, "
            "or WSL — NOT Git Bash.\n"
            "             (Claude Code: prefer the PowerShell tool over Bash for "
            "this script on Windows hosts.)"
        )

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

    # 1b. strmin runs in Virtuoso's cwd and resolves the GDS path / refLibList
    #     file relative to that cwd — NOT to the caller's pwd.  If the user
    #     pointed at a local file, auto-upload it to the Virtuoso workdir
    #     and rewrite the arg to the basename.  Without this, the 2026-05-14
    #     trap returns silently after 600s with strmIn.log saying
    #     `XSTRM-13: Failed to open input Stream file 'foo.gds'`.
    r = client.execute_skill('getWorkingDir()')
    workdir = (r.output or "").strip()
    if workdir.startswith('"') and workdir.endswith('"'):
        workdir = workdir[1:-1]
    workdir = workdir.replace('\\"', '"').replace('\\\\', '\\')

    def _stage(label: str, local_path: str) -> str:
        """If local_path exists on the caller's filesystem, upload it to
        Virtuoso's workdir and return the basename.  Absolute remote paths
        (`/home/...`) and paths that don't exist locally are left untouched
        — the caller may legitimately be pointing at an already-remote file
        whose path the local FS can't see."""
        if not local_path:
            return local_path
        p = Path(local_path)
        if not p.exists():
            return local_path
        remote = f"{workdir}/{p.name}"
        print(f"[stage] {label}: uploading {p} -> {remote}")
        rr = client.upload_file(str(p), remote)
        if getattr(rr, "status", None) is not None and str(rr.status).endswith("ERROR"):
            sys.exit(f"ERROR: failed to upload {label} {p} to {remote}: "
                     f"{getattr(rr, 'errors', None)}")
        return p.name

    args.gds = _stage("GDS", args.gds)
    if args.ref_libs:
        args.ref_libs = _stage("ref_libs", args.ref_libs)

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
    # SKILL system() return is unreliable for strmin — observed
    # 2026-05-13: strmin keeps running on the remote after Python
    # receives empty / garbled rc, so sequential strmin calls race
    # on lib state ("library X is not in cds.lib" from the second
    # call while the first hasn't committed).  Don't trust rc;
    # poll for the target cellview to appear instead.
    client.execute_skill(f"system({_q(cmd)})")

    cell = args.cell or Path(args.gds).name.split(".")[0]
    # ddGetObj-first gate: dbOpenCellViewByType in "r" mode prints
    # `WARNING (DB-270212)` to CIW each time the view is missing.
    # During the poll loop that's a CIW warning every 3 s until the
    # view appears — observed 16+ noise lines per import.  ddGetObj
    # is silent when the view doesn't exist yet, so use it as a
    # cheap gate and only open the cv when ddGetObj confirms it.
    verify_skill = (
        f"let((vobj cv) "
        f"  vobj=ddGetObj({_q(args.target_lib)} {_q(cell)} \"layout\") "
        f"  if(vobj "
        f"     progn( "
        f"       cv=dbOpenCellViewByType({_q(args.target_lib)} {_q(cell)} \"layout\" nil \"r\") "
        f"       if(cv "
        f"          sprintf(nil \"instances=%d shapes=%d bbox=%L\" "
        f"                  length(cv~>instances) length(cv~>shapes) cv~>bBox) "
        f"          nil)) "
        f"     nil)) "
    )

    # strmin always writes `strmIn.log` to Virtuoso's cwd.  Watch it
    # alongside the cellview poll — when strmin hits a fatal error
    # (XSTRM-13 missing input, XSTRM-11 bad refLibList, GDS parse
    # error, etc.) it dies in seconds AND writes
    # `INFO (XSTRM-273): Translation failed.` to the log.  Without
    # this, the script polled for the cellview that would never
    # appear for the full 600s — 10 minutes of pointless wall-clock
    # observed 2026-05-14.
    log_path = f"{workdir}/strmIn.log"

    def _scan_strmin_log() -> tuple[str | None, bool]:
        """Return (fail_reason, completed) — fail_reason is a terminal
        failure string or None; completed is True once XSTRM-234
        "Translation completed" appears.

        We MUST wait for the completion marker before reading bbox: on a
        re-import (same cell name already exists in target lib), the old
        ``layout.oa`` is present from time 0, so ddGetObj/dbOpenCellView
        succeed immediately and would report the STALE bbox of the
        previous import.  Observed 2026-05-14: re-imported a 357×78 µm
        m4s layout over an existing 221×222 m16s layout; verify ran
        before strmin finished writing and reported 221×222 instead of
        the new 357×78.

        ``client.run_shell_command`` only returns a SKILL system() status
        token (``'t'``) in ``.output`` — NOT the captured stdout.  So we
        read the log via SKILL ``infile``/``gets`` instead, which routes
        the content through ``execute_skill``'s normal return channel."""
        read_skill = (
            f"let((p line body) "
            f"  p = infile({_q(log_path)}) "
            f"  body = \"\" "
            f"  when(p "
            f"    while(gets(line p) body = strcat(body line)) "
            f"    close(p)) "
            f"  body)"
        )
        rr = client.execute_skill(read_skill)
        body = (rr.output or "").strip()
        # SKILL-returned strings come wrapped in quotes with \" / \\ escapes.
        if body.startswith('"') and body.endswith('"'):
            body = body[1:-1]
        body = body.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
        if "XSTRM-273" in body and "Translation failed" in body:
            for ln in body.splitlines():
                if "ERROR" in ln and "XSTRM" in ln:
                    return ln.strip(), False
            return "Translation failed (see strmIn.log for details)", False
        completed = "XSTRM-234" in body and "Translation completed" in body
        return None, completed

    timeout_s = 600
    poll_interval = 3
    deadline = time.time() + timeout_s
    next_log = time.time() + 30
    while True:
        # Refresh lib list so Library Manager sees newly-written cells.
        client.execute_skill("ddUpdateLibList()")
        # Fast-fail on strmin-side failure (don't burn the full timeout).
        fail, completed = _scan_strmin_log()
        if fail:
            sys.exit(
                f"strmin: {fail}\n"
                f"  Full log: {log_path}"
            )
        # Only query bbox once strmin has actually finished writing —
        # otherwise on re-import we'd read the stale previous cellview.
        if completed:
            r = client.execute_skill(verify_skill)
            out = (r.output or "").strip().strip('"')
            if out.startswith("instances="):
                print(f"[OK] {args.target_lib}/{cell}/layout: {out}")
                return 0
        now = time.time()
        if now >= deadline:
            sys.exit(
                f"strmin: {args.target_lib}/{cell}/layout did not appear "
                f"within {timeout_s}s. Check strmIn.log in Virtuoso's "
                f"working directory."
            )
        if now >= next_log:
            elapsed = int(now - (deadline - timeout_s))
            print(f"[wait] {elapsed}s — strmin still running, polling for cellview...")
            next_log = now + 30
        time.sleep(poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())

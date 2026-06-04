#!/usr/bin/env python3
"""Reproduce / disprove issue #99 against a live Virtuoso.

Issue #99 says `read_schematic()` returns
``{instances:[], nets:{}, pins:{}, notes:[]}`` for a populated cellview
(AI_LIB/L3_FCT) whose direct DFII probe reports 181 instances /
18 terminals / 123 nets / 1215 shapes.

This script does three things on a real Virtuoso, against any cell you
have access to, and decides whether the bug fires on YOUR cell:

  1. Direct DFII probe.  Opens the cellview via the same
     ``dbOpenCellViewByType(... "schematic" "schematic" "r")`` call the
     bridge uses, then counts ``cv~>instances/terminals/nets/shapes``
     and dumps ``cv~>viewType``.  This is the "ground truth" the issue
     reporter used.
  2. Bridge ``read_schematic()``.  The code path under test.
  3. Side-by-side comparison + verdict.

If the two disagree -- the bug reproduces.  In that case the script
ALSO dumps the first 2 KB of the raw SKILL output the bridge received,
so you can see at a glance whether the body is

  * ``ERROR`` (cv was nil under the bridge -- e.g. lib path differs),
  * empty (transport / IPC ate the body),
  * truncated mid-stanza (output too large or a foreach blew up
    halfway through), or
  * fully populated but mis-parsed (parser bug -- unlikely, pinned
    green by tests/test_schematic_reader.py).

Usage::

    # Reproduce on the cell from the issue (only works if you have it):
    python 99_repro_issue99.py AI_LIB L3_FCT

    # Sanity check on a cell you know is fine:
    python 99_repro_issue99.py myLib myInverter

    # Skip param filtering -- catches CDF-driven failures:
    python 99_repro_issue99.py AI_LIB L3_FCT --no-filters

The exit code is 0 when bridge and DFII agree, 1 when they disagree
(the bug fired), and 2 when DFII itself returned nil (cellview could
not be opened at all).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from virtuoso_bridge import VirtuosoClient, decode_skill_output
from virtuoso_bridge.virtuoso.schematic.reader import (
    _GEOMETRY_INST_EXPR,
    _NOTES_SECTION_EXPR,
    _SKILL_TOPOLOGY,
    read_schematic,
)


# =======================================================================
# Direct DFII probe -- the "ground truth" side
# =======================================================================

_DFII_PROBE = r'''
let((lib cell cv libPath cellViews vt bbox ni nt nn ns)
  ddUpdateLibList()
  lib = ddGetObj("{lib}")
  cell = ddGetObj("{lib}" "{cell}")
  cv = dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "r")
  libPath  = if(lib lib~>readPath "<no-lib>")
  cellViews = if(cell buildString(cell~>views~>name " ") "<no-cell>")
  if(cv
    progn(
      vt   = if(cv~>viewType cv~>viewType "nil")
      bbox = sprintf(nil "%L" cv~>bBox)
      ni   = length(cv~>instances)
      nt   = length(cv~>terminals)
      nn   = length(cv~>nets)
      ns   = length(cv~>shapes)
      sprintf(nil
        "DFII_OK\nlibPath=%s\nviews=%s\nviewType=%s\nbBox=%s\ninstances=%d\nterminals=%d\nnets=%d\nshapes=%d\n"
        libPath cellViews vt bbox ni nt nn ns))
    sprintf(nil
      "DFII_NIL\nlibPath=%s\nviews=%s\n" libPath cellViews)))
'''


def _dfii_probe(client: VirtuosoClient, lib: str, cell: str) -> dict[str, Any]:
    """Direct ``length(cv~>...)`` probe; returns parsed counts."""
    skill = _DFII_PROBE.format(lib=lib, cell=cell)
    r = client.execute_skill(skill, timeout=30)
    raw = decode_skill_output(r.output)

    out: dict[str, Any] = {"raw": raw, "ok": False}
    for line in raw.splitlines():
        line = line.strip()
        if line == "DFII_OK":
            out["ok"] = True
        elif line == "DFII_NIL":
            out["ok"] = False
        elif "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    # Numeric coercion for the counts
    for key in ("instances", "terminals", "nets", "shapes"):
        if key in out:
            try:
                out[key] = int(out[key])
            except (TypeError, ValueError):
                pass
    return out


# =======================================================================
# Mirror the bridge's exact SKILL expression so we can capture the
# raw output it would see (without going through the parser).
# =======================================================================

def _bridge_raw_skill_output(
    client: VirtuosoClient,
    lib: str,
    cell: str,
    *,
    include_positions: bool = False,
) -> str:
    """Execute the bridge's own _SKILL_TOPOLOGY directly and return raw."""
    cv_expr = (
        f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "r")'
    )
    skill = _SKILL_TOPOLOGY.replace("{cv_expr}", cv_expr)
    skill = skill.replace(
        "{geometry_inst}", _GEOMETRY_INST_EXPR if include_positions else ""
    )
    skill = skill.replace("{notes_section}", _NOTES_SECTION_EXPR)
    r = client.execute_skill(skill, timeout=60)
    return decode_skill_output(r.output)


# =======================================================================
# Reporting
# =======================================================================

def _print_dfii(probe: dict[str, Any]) -> None:
    print("=" * 64)
    print("[1] Direct DFII probe (ground truth)")
    print("=" * 64)
    if not probe.get("ok"):
        print(f"  cv = NIL  -- dbOpenCellViewByType returned nil.")
        print(f"  libPath = {probe.get('libPath', '?')}")
        print(f"  views   = {probe.get('views', '?')}")
        print()
        print("  Without an openable cv there is no ground truth to compare.")
        print("  Fix the lib path / view name first, then re-run.")
        return
    print(f"  libPath   = {probe.get('libPath')}")
    print(f"  views     = {probe.get('views')}")
    print(f"  viewType  = {probe.get('viewType')}   "
          f"<-- nil here is what issue #99 calls out")
    print(f"  bBox      = {probe.get('bBox')}")
    print(f"  instances = {probe.get('instances')}")
    print(f"  terminals = {probe.get('terminals')}")
    print(f"  nets      = {probe.get('nets')}")
    print(f"  shapes    = {probe.get('shapes')}")


def _print_bridge(data: dict[str, Any]) -> None:
    print()
    print("=" * 64)
    print("[2] Bridge read_schematic()")
    print("=" * 64)
    print(f"  instances = {len(data.get('instances', []))}")
    print(f"  nets      = {len(data.get('nets', {}))}")
    print(f"  pins      = {len(data.get('pins', {}))}")
    print(f"  notes     = {len(data.get('notes', []))}")


def _verdict(probe: dict[str, Any], data: dict[str, Any]) -> int:
    """Return exit code: 0 agree, 1 disagree (bug), 2 DFII nil."""
    print()
    print("=" * 64)
    print("[3] Verdict")
    print("=" * 64)
    if not probe.get("ok"):
        print("  DFII could not open the cellview -- nothing to compare.")
        print("  Result: INCONCLUSIVE.  Exit code = 2.")
        return 2

    dfii_inst = probe.get("instances", 0)
    bridge_inst = len(data.get("instances", []))
    dfii_nets = probe.get("nets", 0)
    bridge_nets = len(data.get("nets", {}))
    dfii_pins = probe.get("terminals", 0)
    bridge_pins = len(data.get("pins", {}))

    rows = [
        ("instances", dfii_inst, bridge_inst),
        ("nets",      dfii_nets, bridge_nets),
        ("pins",      dfii_pins, bridge_pins),
    ]
    print(f"  {'metric':<12} {'DFII':>8} {'bridge':>8}  {'?':>6}")
    print("  " + "-" * 38)
    bug_fired = False
    for label, d, b in rows:
        ok = (d == b) or (d > 0 and b > 0)  # close enough for "bridge saw it"
        mark = "OK" if ok else "MISMATCH"
        if not ok:
            bug_fired = True
        print(f"  {label:<12} {d:>8} {b:>8}  {mark:>8}")

    if bug_fired and bridge_inst == 0 and dfii_inst > 0:
        print()
        print("  >>> ISSUE #99 REPRODUCED on this cell. <<<")
        print("  Bridge returned empty for a non-empty cellview.")
        return 1
    if bug_fired:
        print()
        print("  Counts disagree but not in the 'silently empty' way #99")
        print("  describes.  Possibly a different bug -- investigate.")
        return 1
    print()
    print("  Bridge and DFII agree.  Issue #99 does NOT fire here.")
    return 0


def _dump_raw_evidence(raw: str, limit: int = 2048) -> None:
    print()
    print("=" * 64)
    print(f"[4] Bridge's raw SKILL output (first {limit} bytes)")
    print("=" * 64)
    snippet = raw[:limit]
    print(snippet if snippet else "<empty>")
    if len(raw) > limit:
        print(f"  ... (+{len(raw) - limit} more bytes)")
    print()
    print("Quick read of this output:")
    if raw.strip() == "":
        print("  EMPTY -- IPC / transport likely truncated the response, "
              "or execute_skill swallowed the body.")
    elif raw.strip() == "ERROR":
        print("  ERROR marker -- the bridge's `unless(cv return(\"ERROR\"))`")
        print("  prelude fired, so under the bridge's session cv was nil.")
        print("  Compare cdsLibMgr / lib path against the DFII probe above.")
    elif "INSTANCES" in raw and "END" not in raw:
        print("  TRUNCATED -- INSTANCES header present but no END marker.")
        print("  SKILL probably errored mid-foreach (CDF lookup, terminal")
        print("  scrape, ...) or the response exceeded the transport limit.")
    elif "INSTANCES" in raw and "INST|" not in raw.split("NETS", 1)[0]:
        print("  HEADERS-ONLY -- structure is there but no INST lines.")
        print("  Likely `cv~>instances` came back empty under the bridge,")
        print("  or every instance was filtered by `purpose != \"pin\"`.")
    else:
        print("  Looks fully populated; if counts still mismatch the parser")
        print("  is the suspect.  Re-run tests/test_schematic_reader.py.")


# =======================================================================
# Main
# =======================================================================

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("lib")
    ap.add_argument("cell")
    ap.add_argument("--no-filters", action="store_true",
                    help="disable CDF param filtering")
    ap.add_argument("--no-pos", action="store_true",
                    help="omit positions (matches issue reporter's call)")
    args = ap.parse_args(argv)

    client = VirtuosoClient.from_env()

    probe = _dfii_probe(client, args.lib, args.cell)
    _print_dfii(probe)

    kwargs: dict[str, Any] = {"include_positions": not args.no_pos}
    if args.no_filters:
        kwargs["param_filters"] = None
    data = read_schematic(client, args.lib, args.cell, **kwargs)
    _print_bridge(data)

    rc = _verdict(probe, data)
    if rc == 1 and len(data.get("instances", [])) == 0:
        # Bridge said empty -- dump the smoking gun.
        raw = _bridge_raw_skill_output(
            client, args.lib, args.cell,
            include_positions=not args.no_pos,
        )
        _dump_raw_evidence(raw)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

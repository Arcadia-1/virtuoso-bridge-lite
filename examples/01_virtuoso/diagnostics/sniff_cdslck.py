#!/usr/bin/env python3
"""Find ``.cdslck`` lock files in an OA library tree and report their owners.

Use case — *"why won't this view open?"*:
  Cadence creates ``.cdslck`` lock files when a cellview is held in
  edit mode (or, sometimes, when a previous session crashed without
  cleaning up).  ``maeGetSessions`` / SKILL-side enumeration can be
  unreliable when the holder is on a different Virtuoso process or has
  partially exited; reading the lock files **directly on the
  filesystem** is more authoritative.

What this script does:
  1. SSH ``find`` for every ``.cdslck`` file under the library root
     (``<lib readPath>``).
  2. For each, read the contents (``cat``) — Cadence writes a one-line
     ``owner@host:pid:start_time`` record.
  3. Print a table of (cellview, owner, host, pid, age).

What it does **not** do:
  - It does **not** delete locks.  If you want to break a stale lock,
    confirm with ``ps -p <pid>`` on the lock's host first; only then
    ``rm`` the file.  Deleting a live lock corrupts the cellview.

Usage::

    python sniff_cdslck.py <LIB>
    python sniff_cdslck.py <LIB> --view maestro      # filter to maestro views only

The library must be registered in the remote's ``cds.lib`` (which it
already is if Virtuoso opens it).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient


def _resolve_lib_path(client: VirtuosoClient, lib: str) -> str:
    r = client.execute_skill(f'ddGetObj("{lib}")~>readPath')
    out = (r.output or "").strip().strip('"')
    if not out or out == "nil":
        raise RuntimeError(
            f"could not resolve readPath for library {lib!r} "
            f"(is it in the remote cds.lib?)"
        )
    return out


def _find_locks(client: VirtuosoClient, lib_path: str) -> list[str]:
    """Run ``find`` on the remote; return absolute paths to every .cdslck."""
    r = client.execute_skill(
        f'system("find {lib_path} -name \\".cdslck\\" -print")', timeout=60
    )
    if r.errors:
        raise RuntimeError(f"find failed on remote: {r.errors[0]}")
    # SKILL system() returns nil/t (success/failure indicator), not stdout.
    # Run it via the remote shell directly so we get stdout.
    cmd = client.ssh_runner.run(f"find {lib_path} -name '.cdslck' -print 2>/dev/null")
    return [line.strip() for line in (cmd.stdout or "").splitlines() if line.strip()]


def _read_lock(client: VirtuosoClient, path: str) -> str:
    cmd = client.ssh_runner.run(f"cat {path} 2>/dev/null")
    return (cmd.stdout or "").strip()


def _stat_age(client: VirtuosoClient, path: str) -> float | None:
    """Return the lock file's age in seconds, or None if stat fails."""
    cmd = client.ssh_runner.run(f"stat -c %Y {path} 2>/dev/null")
    try:
        mtime = int((cmd.stdout or "").strip())
        return time.time() - mtime
    except (TypeError, ValueError):
        return None


def _format_age(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("lib", help="OA library name registered in remote cds.lib")
    p.add_argument(
        "--view", default=None,
        help="only show locks under this view (e.g. maestro, layout)",
    )
    args = p.parse_args()

    client = VirtuosoClient.from_env()
    lib_path = _resolve_lib_path(client, args.lib)
    print(f"library {args.lib!r}  →  {lib_path}\n")

    locks = _find_locks(client, lib_path)
    if args.view:
        locks = [p for p in locks if f"/{args.view}/" in p or p.endswith(f"/{args.view}/.cdslck")]

    if not locks:
        print("no .cdslck files found")
        return 0

    # Strip the lib_path prefix for readable output; show owner + age.
    print(f"{'cellview':<55}  {'owner':<25}  {'age':>6}")
    print("-" * 90)
    for lock_path in sorted(locks):
        rel = lock_path
        if rel.startswith(lib_path + "/"):
            rel = rel[len(lib_path) + 1:]
        # Drop the trailing .cdslck so the row identifies the cellview.
        rel = rel.rsplit("/", 1)[0]

        contents = _read_lock(client, lock_path)
        age = _stat_age(client, lock_path)
        age_str = _format_age(age) if age is not None else "?"
        print(f"{rel:<55}  {contents:<25}  {age_str:>6}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

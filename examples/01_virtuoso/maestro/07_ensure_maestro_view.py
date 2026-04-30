#!/usr/bin/env python3
"""Bootstrap a missing ``maestro`` cellview before opening it for GUI work.

``open_gui_session`` assumes the maestro view *already exists* on disk.
For a brand-new testbench cell that has never been opened in Maestro
before, the directory ``<lib>/<cell>/maestro/`` does not exist; calling
``deOpenCellView(... "a")`` returns ``nil`` and Virtuoso pops a
``"Data file does not exist"`` GUI dialog that blocks the SKILL channel.

This example demonstrates the **precondition** to run before
``open_gui_session`` on any cell whose maestro view might be missing.
The fix is two SKILL calls that walk the on-disk state up from nothing:

    maeOpenSetup(<lib> <cell> "maestro")    ; creates master.tag + maestro.sdb in memory
    maeSaveSetup(?session sess)             ; flushes them to disk

After that, the directory layout is what the GUI path expects, and
``open_gui_session`` succeeds without dialog drama.

Idempotent: if the maestro view already exists, ``maeOpenSetup`` simply
re-attaches to it; the ``maeSaveSetup`` is a no-op on disk for an
already-saved view.

Usage::

    python 07_ensure_maestro_view.py <LIB> <CELL>

The cell must already exist (with at least a schematic / symbol).  This
script does **not** create the testbench schematic itself — that's the
schematic-creation examples' job.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro.lifecycle import (
    open_gui_session,
    close_gui_session,
    close_session,
)


def ensure_maestro_view(client: VirtuosoClient, lib: str, cell: str) -> None:
    """Create or attach to ``<lib>/<cell>/maestro``, then save to disk.

    On a fresh cell this is what creates the directory + master.tag +
    maestro.sdb files.  On an existing cell it's effectively a no-op.
    """
    # maeOpenSetup returns the new session string; we don't need it
    # past the save call below.
    r = client.execute_skill(
        f'maeOpenSetup("{lib}" "{cell}" "maestro")', timeout=60
    )
    if r.errors or not r.output or r.output.strip() in ("nil", ""):
        raise RuntimeError(f"maeOpenSetup failed for {lib}/{cell}: {r.errors}")
    # Output is a quoted string like "fnxSession12".
    session = r.output.strip().strip('"')

    # Flush to disk.  Without this, the in-memory session has no
    # persistent files and the next process can't see it.
    rs = client.execute_skill(
        f'maeSaveSetup(?session "{session}")', timeout=30
    )
    if rs.errors:
        raise RuntimeError(f"maeSaveSetup failed: {rs.errors}")

    # Drop the background session — we don't need it.  The on-disk
    # files persist.
    close_session(client, session)


def main() -> int:
    if len(sys.argv) < 3:
        print(
            f"Usage: python {Path(__file__).name} <LIB> <CELL>\n"
            f"Example: python {Path(__file__).name} PLAYGROUND_LLM TB_FOO",
            file=sys.stderr,
        )
        return 1

    lib, cell = sys.argv[1], sys.argv[2]
    client = VirtuosoClient.from_env()

    print(f"[step 1/2] ensure_maestro_view({lib!r}, {cell!r})")
    ensure_maestro_view(client, lib, cell)

    print(f"[step 2/2] open_gui_session — should now succeed without dialog")
    session = open_gui_session(client, lib, cell)
    print(f"           opened: {session!r}")

    # Tidy up so the example leaves no stuck GUI window behind.
    close_gui_session(client, session, save=False)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

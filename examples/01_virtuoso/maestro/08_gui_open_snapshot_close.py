#!/usr/bin/env python3
"""Open a maestro in GUI, snapshot it, then close it.

Complements ``09_snapshot_with_metrics.py`` (which snapshots whichever
maestro window the user currently has focused).  This script owns the
whole lifecycle:

    1. open_gui_session(lib, cell)   -> opens GUI, focuses the window
    2. snapshot(client, output_root) -> resolves and dumps all artifacts
    4. close_gui_session(session)    -> clean close (saves if dirty)

Usage::

    python 08_gui_open_snapshot_close.py <LIB> <CELL>

Running from VSCode without args will NOT work — VSCode doesn't pass
positional arguments by default.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import (
    close_gui_session,
    open_gui_session,
    snapshot,
)


OUTPUT_ROOT = Path(__file__).parent.parent.parent.parent / "output"


def main() -> int:
    if len(sys.argv) < 3:
        print(
            f"Usage: python {Path(__file__).name} <LIB> <CELL>\n"
            f"Example: python {Path(__file__).name} PLAYGROUND_AMP TB_AMP_5T_D2S_DC_AC",
            file=sys.stderr,
        )
        return 1

    lib, cell = sys.argv[1], sys.argv[2]
    client = VirtuosoClient.from_env()

    session = open_gui_session(client, lib, cell)
    print(f"Opened: {lib}/{cell}  (session {session})")

    try:
        snap = snapshot(client, output_root=str(OUTPUT_ROOT))
        if snap.get("session") != session:
            # The opened window should be focused and match the returned
            # session.  Mismatch means something else grabbed focus.
            raise RuntimeError(
                f"Focused window session ({snap.get('session')}) does not "
                f"match the one we just opened ({session}). "
                "Another window may have grabbed focus."
            )

        snap_dir = snap.get("output_dir")
        print(f"Wrote snapshot to: {snap_dir}")
    finally:
        close_gui_session(client, session)
        print(f"Closed session {session}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

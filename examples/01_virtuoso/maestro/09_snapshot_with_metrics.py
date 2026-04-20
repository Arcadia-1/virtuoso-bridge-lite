#!/usr/bin/env python3
"""Snapshot the currently-focused maestro via two primitives.

    1. snapshot(client, output_root=...)  →  identify and dump artifacts

Writes ``{OUTPUT_ROOT}/{YYYYMMDD_HHMMSS}__{lib}__{cell}/`` with three
"tracks" of state — each derived from a different source so they never
overlap:

  * ``state_from_skill.json``         — live SKILL session
  * ``state_from_sdb.xml``            — filtered ``maestro.sdb``
  * ``state_from_active_state.xml``   — filtered ``active.state``

Plus raw copies (``maestro.sdb``, ``active.state``), run history
metadata (``histories.json``, ``latest_history.json``) and a
``<history_name>/`` subdir with the newest run's artifacts.  In debug
mode also ``raw_skill.json`` + ``probe_log.json``.

``scratch_root`` is auto-detected from the downloaded ``maestro.sdb`` —
no configuration needed.  Detection failures simply skip the scratch-
dependent enrichment (histories run paths, spectre.out tail) without
raising.  Pass ``scratch_root=...`` explicitly to override.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import snapshot


OUTPUT_ROOT = Path(__file__).parent.parent.parent.parent / "output"


def main() -> int:
    client = VirtuosoClient.from_env()

    # 1) "Where am I?" + 2) "Dump what I see."
    snap = snapshot(client, output_root=str(OUTPUT_ROOT))
    if not snap.get("session"):
        # Something IS focused — just not a maestro window.  Report it so
        # the user knows exactly what to click away from.
        raise RuntimeError(
            "Focused window is not an ADE Assembler / Explorer maestro.\n"
            "  Click an ADE Assembler/Explorer window and retry."
        )
    print(
        f"Focused: {snap['lib']}/{snap['cell']}/{snap['view']}  "
        f"(session {snap['session']}, {snap.get('app') or 'unknown'})"
    )
    snap_dir = snap.get("output_dir")
    print(f"Wrote snapshot to: {snap_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

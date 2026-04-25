#!/usr/bin/env python3
"""Read the currently focused maestro into one in-memory dict.

Usage:
    1. Open (or click to focus) a maestro view in Virtuoso GUI
    2. Run this script
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import snapshot


def main() -> int:
    client = VirtuosoClient.from_env()
    snap = snapshot(client)
    if not snap.get("session"):
        print(
            "Focused window is not an ADE Assembler / Explorer maestro.\n"
            "  Please focus a maestro window and retry.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Focused: {snap['lib']}/{snap['cell']}/{snap['view']}  "
        f"(session {snap['session']}, {snap.get('app') or 'unknown'})"
    )
    print(json.dumps(snap, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

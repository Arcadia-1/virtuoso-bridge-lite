#!/usr/bin/env python3
"""Load a SKILL .il file into Virtuoso CIW.

Prerequisites:
- virtuoso-bridge tunnel running (virtuoso-bridge start)
- RAMIC daemon loaded in Virtuoso CIW

Customize SONNET_IL below to point to the .il file you want to load.
"""

import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))
from pathlib import Path
from _timing import print_elapsed
from virtuoso_bridge import VirtuosoClient

# ----------------------------------------------------------------------
# Customize: path to the .il SKILL file to load
# ----------------------------------------------------------------------
SONNET_IL = Path(__file__).resolve().parent.parent / "assets" / "sonnet18.il"
# ----------------------------------------------------------------------

client = VirtuosoClient.from_env()
result = client.load_il(SONNET_IL)


print_elapsed("load_il", result.execution_time or 0.0)
upload_tag = "uploaded" if result.metadata.get("uploaded") else "cache hit"
print(f"[{upload_tag}]")
print(f"[OK] local:  {SONNET_IL}")
print(f"[OK] remote: {result.metadata.get('skill_command')}")

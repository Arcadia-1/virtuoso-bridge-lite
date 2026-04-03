#!/usr/bin/env python3
"""Create an RC low-pass filter schematic via execute_operations.

Each step in the operations list maps directly to one atomic SKILL procedure,
making the build sequence explicit and easy to modify.

Circuit: VDC (0.8 V) → R0 (res) → OUT → C0 (cap) → GND

Set VB_DEFAULT_LIB to control which library the cell is created in.

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - RAMIC daemon loaded in Virtuoso CIW
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))

from datetime import datetime

from _timing import format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

IL_FILE = pathlib.Path(__file__).resolve().parent.parent / "assets" / "schematic_ops.il"


def _decode(raw: str) -> str:
    text = (raw or "").strip().strip('"')
    return text.replace("\\n", "\n").replace('\\"', '"')


def main() -> int:
    lib  = os.environ.get("VB_DEFAULT_LIB", "PLAYGROUND_LLM")
    cell = f"rc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    client = VirtuosoClient.from_env()

    load_result = client.load_il(IL_FILE)
    meta = load_result.metadata
    print(f"[load_il] {'uploaded' if meta.get('uploaded') else 'cache hit'}"
          f"  [{format_elapsed(load_result.execution_time or 0.0)}]")

    print(f"Library : {lib}")
    print(f"Cell    : {cell}")

    commands = [
        f'SchOpenNew("{lib}" "{cell}")',                    # 1. create + open schematic
        'SchCreateAnalogInst("vdc" "V0" 3.0 0.0 "R0")',   # 2. VDC supply
        'SchCreateAnalogInst("res" "R0" 0.0 0.0 "R0")',   # 3. resistor
        'SchCreateAnalogInst("cap" "C0" 1.5 0.0 "R0")',   # 4. capacitor
        'SchSetInstParam("V0" "vdc" "800m")',               # 5. VDD = 0.8 V
        'SchNetLabel("V0" "PLUS"  "VDD")',                  # 6. label V0+
        'SchNetLabel("V0" "MINUS" "GND")',                  # 7. label V0-
        'SchNetLabel("R0" "PLUS"  "VDD")',                  # 8. label R0+
        'SchNetLabel("R0" "MINUS" "OUT")',                  # 9. label R0-
        'SchNetLabel("C0" "PLUS"  "OUT")',                  # 10. label C0+
        'SchNetLabel("C0" "MINUS" "GND")',                  # 11. label C0-
        'SchSave()',                                        # 12. schCheck + save
    ]

    elapsed, result = timed_call(lambda: client.execute_operations(commands, timeout=30))
    print(f"[execute_operations] [{format_elapsed(elapsed)}]")

    output = _decode(result.output or "")
    errors = result.errors or []
    if output:
        print(output)
    for e in errors:
        print(f"[error] {e}")
    if not output and not errors:
        print(f"[status] {result.status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

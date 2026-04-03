#!/usr/bin/env python3
"""Capture a screenshot of the current Virtuoso window (layout or schematic).

Usage::

    python 04_screenshot.py

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
  - A cellview open in Virtuoso
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "screenshot.il"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def main() -> int:
    client = VirtuosoClient.from_env()

    elapsed, design = timed_call(client.get_current_design)
    print(f"[get_current_design] [{format_elapsed(elapsed)}]")
    lib, cell, view = design
    if not lib or not cell:
        print("No active design in Virtuoso. Open a cellview first.")
        return 1
    print(f"Design: {lib}/{cell}/{view}")

    load_elapsed, load_result = timed_call(lambda: client.load_il(IL_FILE))
    meta = load_result.metadata
    print(f"[load_il] {'uploaded' if meta.get('uploaded') else 'cache hit'}  [{format_elapsed(load_elapsed)}]")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_path = f"/tmp/virtuoso_bridge_screenshots/{cell}_{stamp}.png"
    shell_elapsed, _ = timed_call(
        lambda: client.run_shell_command("mkdir -p /tmp/virtuoso_bridge_screenshots", timeout=10)
    )
    print(f"[run_shell_command] [{format_elapsed(shell_elapsed)}]")

    exec_elapsed, result = timed_call(
        lambda: client.execute_skill(
            f'takeScreenshot("{escape_skill_string(remote_path)}")', timeout=20
        )
    )
    print(f"[execute_skill] [{format_elapsed(exec_elapsed)}]")
    print(decode_skill(result.output or ""))

    local_path = OUTPUT_DIR / Path(remote_path).name
    download_elapsed, dl_result = timed_call(
        lambda: client.download_file(remote_path, local_path, timeout=30)
    )
    if not dl_result.ok:
        print(f"[download] failed: {dl_result.errors[0] if dl_result.errors else 'request failed'}")
        return 1
    print(f"[download] [{format_elapsed(download_elapsed)}]")
    print(f"Local screenshot: {dl_result.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

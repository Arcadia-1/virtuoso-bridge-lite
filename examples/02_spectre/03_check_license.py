#!/usr/bin/env python3
"""Check Spectre license availability on the remote host.

Queries the remote server for Spectre binary path, version, and active
license usage via ``SpectreSimulator.check_license()``.

Usage::

    python examples/02_spectre/03_check_license.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _result_io import save_summary_json
from virtuoso_bridge.models import ExecutionStatus, SimulationResult
from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output" / "check_license"


def _format_licenses(lines: list[str]) -> None:
    if not lines:
        print("  (no active license usage found)")
        return
    for line in lines:
        print(f"  {line}")


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sim = SpectreSimulator.from_env(
        spectre_cmd=os.getenv("SPECTRE_CMD", "spectre"),
        spectre_args=spectre_mode_args("ax"),
        work_dir=OUT_DIR,
        output_format="psfascii",
    )

    print("[Check] Querying Spectre license status on remote host ...\n")
    info = sim.check_license()

    ok = info.get("ok", False)
    spectre_path = info.get("spectre_path")
    version = info.get("version")
    licenses = info.get("licenses", [])
    raw_output = info.get("raw_output", "")
    stderr = info.get("stderr", "")

    print("=" * 55)
    print(f"  Spectre found    : {'YES' if spectre_path else 'NO'}")
    if spectre_path:
        print(f"  Path             : {spectre_path}")
    if version:
        print(f"  Version          : {version}")
    print(f"  Active licenses  :")
    _format_licenses(licenses)
    print(f"  Overall status   : {'OK' if ok else 'NOT OK'}")
    print("=" * 55)

    if raw_output:
        print(f"\n[Raw stdout]\n{raw_output}")
    if stderr:
        print(f"\n[Raw stderr]\n{stderr}")

    save_summary_json(
        SimulationResult(status=ExecutionStatus.SUCCESS if ok else ExecutionStatus.FAILURE),
        OUT_DIR / "license_check.json",
        extra=info,
    )
    print(f"\n[Summary] {OUT_DIR / 'license_check.json'}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

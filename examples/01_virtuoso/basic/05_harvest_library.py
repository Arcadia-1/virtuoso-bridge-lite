#!/usr/bin/env python3
"""Harvest library metadata: cells, views, Maestro setups, and result paths.

Handles libraries that use non-standard session views (adexl, maestro2, etc.),
normalizes nil SKILL responses, and resolves library paths via fallback chain.

Usage::

    python 05_harvest_library.py NEX_ADC_export        # harvest one library
    python 05_harvest_library.py NEX_ADC_export -o out  # save JSON to out/
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed
from virtuoso_bridge import VirtuosoClient

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "harvest_library.il"


def _skill_list(raw: str) -> list[str]:
    """Parse a SKILL list like '("maestro" "adexl")' into Python list.

    Also normalizes nil / empty to [].
    """
    text = (raw or "").strip().strip('"')
    if not text or text == "nil":
        return []
    # Remove outer parens and split quoted items
    text = text.strip("()")
    return [m.group(1) for m in re.finditer(r'"([^"]*)"', text)]


def harvest_library(client: VirtuosoClient, lib_name: str) -> dict:
    """Harvest library metadata and return structured dict."""

    # Get library root
    r = client.execute_skill(f'HarvestGetLibRoot("{lib_name}")', timeout=20)
    lib_root = decode_skill(r.output)
    print(f"[harvest] Library root: {lib_root or '(not resolved)'}")

    # Get cells
    r = client.execute_skill(
        f'mapcar(lambda((c) c~>name) ddGetObj("{lib_name}")~>cells)', timeout=20
    )
    cells = _skill_list(r.output)
    print(f"[harvest] Found {len(cells)} cells")

    result = {"library": lib_name, "root": lib_root, "cells": {}}

    for cell_name in cells:
        # Get all views
        r = client.execute_skill(
            f'mapcar(lambda((v) v~>name) ddGetObj("{lib_name}" "{cell_name}")~>views)',
            timeout=20,
        )
        all_views = _skill_list(r.output)

        # Classify views
        r = client.execute_skill(
            f"HarvestGetSessionViews('{_skill_list_literal(all_views)})", timeout=20
        )
        session_views = _skill_list(r.output)

        r = client.execute_skill(
            f"HarvestGetSchematicViews('{_skill_list_literal(all_views)})", timeout=20
        )
        schematic_views = _skill_list(r.output)

        cell_info: dict = {
            "all_views": all_views,
            "session_views": session_views,
            "schematic_views": schematic_views,
            "sessions": {},
        }

        # Probe each session view for setups
        for sv in session_views:
            r = client.execute_skill(
                f'HarvestProbeSetups("{lib_name}" "{cell_name}" "{sv}")', timeout=30
            )
            raw = decode_skill(r.output)
            setups_info = [line for line in raw.splitlines() if line.strip()] if raw else []
            cell_info["sessions"][sv] = {
                "setups": setups_info,
                "has_results": False,
            }

            # Check result directory
            if lib_root:
                r = client.execute_skill(
                    f'HarvestProbeResults("{lib_root}" "{cell_name}" "{sv}")', timeout=10
                )
                cell_info["sessions"][sv]["has_results"] = "t" in (r.output or "")

        result["cells"][cell_name] = cell_info
        view_summary = ", ".join(session_views) if session_views else "(none)"
        print(f"  {cell_name:<30} sessions: {view_summary}")

    return result


def _skill_list_literal(items: list[str]) -> str:
    """Convert Python list to SKILL list literal: '("a" "b")."""
    if not items:
        return "()"
    return "(" + " ".join(f'"{item}"' for item in items) + ")"


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python 05_harvest_library.py <library_name> [-o <output_dir>]")
        return 1

    lib_name = sys.argv[1]
    output_dir = None
    if "-o" in sys.argv:
        idx = sys.argv.index("-o")
        if idx + 1 < len(sys.argv):
            output_dir = Path(sys.argv[idx + 1])

    client = VirtuosoClient.from_env()

    # Load harvest SKILL procedures
    load_result = client.load_il(IL_FILE)
    upload_tag = "uploaded" if load_result.metadata.get("uploaded") else "cache hit"
    print(f"[load_il] {upload_tag}  [{format_elapsed(load_result.execution_time or 0.0)}]")

    # Harvest
    data = harvest_library(client, lib_name)

    # Summary
    total_cells = len(data["cells"])
    cells_with_sessions = sum(
        1 for c in data["cells"].values() if c["session_views"]
    )
    cells_with_results = sum(
        1
        for c in data["cells"].values()
        for s in c["sessions"].values()
        if s["has_results"]
    )
    print(f"\n[summary] {total_cells} cells, {cells_with_sessions} with sessions, "
          f"{cells_with_results} with results")

    # Save JSON
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        out_file = output_dir / f"{lib_name}_harvest.json"
        out_file.write_text(json.dumps(data, indent=2))
        print(f"[output] Saved to {out_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

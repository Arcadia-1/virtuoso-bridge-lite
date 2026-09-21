#!/usr/bin/env python3
"""Import an exact-coordinate schematic manifest and capture GUI evidence.

The bundled JSON files are templates.  Replace the placeholder library/cell
names and pin offsets in ``process-map.example.json`` with a writable library
and symbol masters from your installation before running this example.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from virtuoso_bridge import VirtuosoClient


HERE = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=HERE / "source.example.json"
    )
    parser.add_argument(
        "--process-map", type=Path, default=HERE / "process-map.example.json"
    )
    parser.add_argument("--process", action="append", default=[])
    parser.add_argument("--cell", action="append", default=[])
    parser.add_argument("--profile", default=None)
    parser.add_argument("--output-dir", type=Path, default=HERE / "output")
    parser.add_argument("--no-capture", action="store_true")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing target only after staging and verification",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = VirtuosoClient.from_env(profile=args.profile)
    result = client.schematic.import_manifest(
        args.manifest,
        args.process_map,
        processes=args.process or None,
        cells=args.cell or None,
        overwrite=args.overwrite,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.output_dir / "import-result.json"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(result_path)
    if not args.no_capture:
        for output in client.schematic.capture_import_result(result, args.output_dir):
            print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

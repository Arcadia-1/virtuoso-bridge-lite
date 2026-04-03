#!/usr/bin/env python3
"""Dump CDF instance parameters from a schematic.

Usage::

    python 06_read_instance_params.py                        # active schematic, all params
    python 06_read_instance_params.py LIB CELL              # specific cell, all params
    python 06_read_instance_params.py --filter w l nf m     # restrict to named params

"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient

IL_FILE = Path(__file__).resolve().parent.parent / "assets" / "read_instance_params.il"


def main() -> int:
    argv = sys.argv[1:]

    filter_names = None
    if "--filter" in argv:
        idx = argv.index("--filter")
        filter_names = argv[idx + 1:]
        argv = argv[:idx]

    lib  = argv[0] if len(argv) >= 1 else None
    cell = argv[1] if len(argv) >= 2 else None

    args = []
    if lib and cell:
        args += [f'?lib "{lib}"', f'?cell "{cell}"']
    if filter_names:
        quoted = " ".join(f'"{p}"' for p in filter_names)
        args.append(f"?filter list({quoted})")

    client = VirtuosoClient.from_env()

    load_elapsed, load_result = timed_call(lambda: client.load_il(IL_FILE, timeout=20))
    meta = load_result.metadata
    print(f"[load_il] {'uploaded' if meta.get('uploaded') else 'cache hit'}  [{format_elapsed(load_elapsed)}]")

    exec_elapsed, result = timed_call(
        lambda: client.execute_skill(f'CdfDumpInstParams({" ".join(args)})', timeout=30)
    )
    print(f"[execute_skill] [{format_elapsed(exec_elapsed)}]")
    print()

    out = decode_skill(result.output or "")
    print(out or "(empty — open a schematic or pass LIB CELL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

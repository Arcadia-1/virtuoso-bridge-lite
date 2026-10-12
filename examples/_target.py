"""Explicit-target arguments for examples that change or delete design data.

These examples never act on "whatever is currently open": the library and
cell are required, and anything that deletes data only runs with --yes
(otherwise it prints what it would do).
"""

from __future__ import annotations

import argparse


def parse_target(description: str, *, destructive: bool, extra=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("lib", help="library of the target cell")
    parser.add_argument("cell", help="target cell")
    if extra is not None:
        extra(parser)
    if destructive:
        parser.add_argument("--yes", action="store_true",
                            help="perform the deletion (default: dry run)")
    return parser.parse_args()

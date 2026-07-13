#!/usr/bin/env python3
"""Inspect one library and its top-level categories.

Usage::

    python 08_library_management.py MY_LIB
"""

from __future__ import annotations

import sys

from virtuoso_bridge import VirtuosoClient


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python 08_library_management.py <library_name>")
        return 2

    client = VirtuosoClient.from_env()
    library = sys.argv[1]
    info = client.library.get(library)

    print(f"library: {info.name}")
    print(f"path: {info.path}")
    print(f"technology: {info.technology_library or '(unbound)'}")
    print("categories:")
    for category in client.library.list_categories(library):
        cells = client.library.list_category_cells(library, category)
        print(f"  {category}: {', '.join(cells) if cells else '(empty)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Delete all shapes on one layer/purpose from one explicit layout.

Usage::

    python 07_delete_shapes_on_layer.py <LIB> <CELL> [--layer-map M3=METAL3] [--yes]

Without --yes it only reports how many shapes would be deleted. The layer must
exist in the library's technology; customize DELETE_LAYER / DELETE_PURPOSE
below or rename it with --layer-map.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _pdk import add_layer_map_argument, parse_layer_map, rename_layer, require_cell, require_layers
from _target import parse_target
from _timing import decode_skill, format_elapsed, timed_call
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import layout_delete_shapes_on_layer
from virtuoso_bridge.virtuoso.ops import escape_skill_string

# ----------------------------------------------------------------------
# Customize to match the layer/purpose you want to delete
# ----------------------------------------------------------------------
DELETE_LAYER   = "M3"
DELETE_PURPOSE = "drawing"
# ----------------------------------------------------------------------


def main() -> int:
    args = parse_target(
        "Delete all shapes on one layer/purpose from one layout.",
        destructive=True,
        extra=add_layer_map_argument,
    )
    layer = rename_layer(DELETE_LAYER, parse_layer_map(args.layer_map))
    client = VirtuosoClient.from_env()
    require_cell(client, args.lib, args.cell)
    require_layers(client, args.lib, [(layer, DELETE_PURPOSE)])

    lib, cell = escape_skill_string(args.lib), escape_skill_string(args.cell)
    on_layer = (f'setof(s vbCv~>shapes car(s~>lpp) == "{escape_skill_string(layer)}" '
                f'&& cadr(s~>lpp) == "{escape_skill_string(DELETE_PURPOSE)}")')
    if not args.yes:
        count = client.execute_skill(
            f'let((vbCv n) vbCv = dbOpenCellViewByType("{lib}" "{cell}" "layout" "maskLayout" "r") '
            f"n = length({on_layer}) dbClose(vbCv) n)"
        ).output
        print(f"Would delete {count} shape(s) on {layer}/{DELETE_PURPOSE} from "
              f"{args.lib}/{args.cell}; pass --yes to do it.")
        return 0

    delete = layout_delete_shapes_on_layer(layer, DELETE_PURPOSE, cv_expr="vbCv")
    elapsed, result = timed_call(lambda: client.execute_skill(
        f'let((vbCv vbReport) vbCv = dbOpenCellViewByType("{lib}" "{cell}" "layout" "maskLayout" "a") '
        f"vbReport = {delete} dbSave(vbCv) dbClose(vbCv) vbReport)",
        timeout=30,
    ))
    print(f"[layout_delete_shapes_on_layer] [{format_elapsed(elapsed)}]")
    print(decode_skill(result.output or "") or result.errors)
    return 0 if result.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Check an example's PDK layer and cell names against the target library.

The layout examples are written with tsmcN28 names (M1, M2, ..., nch_ulvt_mac).
Before drawing anything they verify that every layer/purpose they use exists
in the target library's technology, and that any PDK master they place exists,
so a different PDK fails with a clear message instead of an error buried in
the CIW or a "successful" edit on layers that do not exist.

    --layer-map M1=METAL1,M2=METAL2   rename the example's layers for your PDK
"""

from __future__ import annotations

import argparse
import re
from typing import Any, Iterable

LAYER_MAP_HELP = "rename the example's layers for your PDK, e.g. M1=METAL1,M2=METAL2"


def add_layer_map_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--layer-map", default="", help=LAYER_MAP_HELP)


def parse_layer_map(text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in (text or "").split(","))):
        old, sep, new = item.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise SystemExit(f"--layer-map: expected OLD=NEW, got {item!r}")
        mapping[old.strip()] = new.strip()
    return mapping


def rename_layer(layer: str, mapping: dict[str, str]) -> str:
    return mapping.get(layer, layer)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def require_layers(client: Any, lib: str, pairs: Iterable[tuple[str, str]]) -> None:
    """Exit unless every (layer, purpose) is valid in *lib*'s technology."""
    output = client.execute_skill(
        f"let((tf) tf = techGetTechFile(ddGetObj({_quote(lib)})) "
        "when(tf mapcar(lambda((lp) list(lp~>name lp~>purpose)) setof(lp tf~>lps lp~>valid))))"
    ).output or ""
    valid = set(re.findall(r'\("([^"]+)" "([^"]+)"\)', output))
    if not valid:
        raise SystemExit(f"{lib} has no technology library bound; bind one before running this example.")
    missing = sorted(set(pairs) - valid)
    if missing:
        drawing = sorted({layer for layer, purpose in valid if purpose == "drawing"})
        hints = [
            f"{layer} -> {' or '.join(similar)}"
            for layer in sorted({layer for layer, _ in missing})
            if (similar := _similar_layers(layer, drawing))
        ]
        listed = ", ".join(drawing[:40]) + (f" (+{len(drawing) - 40} more)" if len(drawing) > 40 else "")
        raise SystemExit(
            f"{lib}'s technology has no {', '.join(f'{l}/{p}' for l, p in missing)}.\n"
            + (f"Similar layers: {'; '.join(hints)}\n" if hints else "")
            + f"Its drawing layers: {listed}\n"
            "Edit the layer constants in this example or pass --layer-map OLD=NEW."
        )


def _similar_layers(layer: str, names: Iterable[str]) -> list[str]:
    """Names with the same first letter and trailing number, e.g. M1 -> METAL1."""
    number = re.search(r"\d+$", layer)
    if not number:
        return []
    ending = re.compile(rf"(?<!\d){number.group()}$")
    return [name for name in names if name[:1].upper() == layer[:1].upper() and ending.search(name)][:3]


def require_cell(client: Any, lib: str, cell: str, view: str = "layout") -> None:
    """Exit unless *lib*/*cell*/*view* exists."""
    found = client.execute_skill(f"ddGetObj({_quote(lib)} {_quote(cell)} {_quote(view)}) != nil").output
    if (found or "").strip() != "t":
        raise SystemExit(f"{lib}/{cell}/{view} does not exist; check the library and cell (see --help).")


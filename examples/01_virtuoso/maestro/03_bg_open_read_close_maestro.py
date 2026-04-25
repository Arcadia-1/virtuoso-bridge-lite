#!/usr/bin/env python3
"""Open a maestro in background, read basic session config, then close it.

Usage::

    python 03_bg_open_read_close_maestro.py <LIB>

    <LIB> is required — the Virtuoso library where the Maestro setup lives.
    Example::

        python 03_bg_open_read_close_maestro.py testlib

    Running this script from VSCode without passing <LIB> will NOT work.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import close_session, open_session

CELL = "TB_AMP_5T_D2S_DC_AC"


def _decode_atom(raw: str | None) -> str:
    text = (raw or "").strip().strip('"')
    return "" if not text or text.lower() == "nil" else text


def main() -> int:
    # ------------------------------------------------------------------
    # Argument check
    # ------------------------------------------------------------------
    if len(sys.argv) < 2:
        print("=" * 60, file=sys.stderr)
        print(" ERROR: missing required argument <LIB>", file=sys.stderr)
        print()
        print(
            f" Usage: python {Path(__file__).name} <LIB>\n"
            " Example: python 03_bg_open_read_close_maestro.py lifangshi\n",
            file=sys.stderr,
        )
        print(
            " NOTE: Running this script from VSCode (Ctrl+F5 / F5) will NOT\n"
            "       work — VSCode does not pass command-line arguments by default.\n",
            file=sys.stderr,
        )
        print("=" * 60, file=sys.stderr)
        return 1

    lib = sys.argv[1]

    client = VirtuosoClient.from_env()

    session = open_session(client, lib, CELL)
    try:
        cfg: dict[str, str] = {"session": session}
        test = _decode_atom(
            client.execute_skill(
                f'maeGetSetup(?session "{session}")', timeout=15
            ).output
        )
        if test:
            cfg["test"] = test
            cfg["lib"] = _decode_atom(
                client.execute_skill(
                    f'maeGetEnvOption("{test}" ?option "lib" ?session "{session}")',
                    timeout=15,
                ).output
            )
            cfg["cell"] = _decode_atom(
                client.execute_skill(
                    f'maeGetEnvOption("{test}" ?option "cell" ?session "{session}")',
                    timeout=15,
                ).output
            )
        print(json.dumps(cfg, indent=2, default=str))
    finally:
        close_session(client, session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

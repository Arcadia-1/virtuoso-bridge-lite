#!/usr/bin/env python3
"""Switch a Maestro test from APS (default) to Spectre X / LX / MX / AX / VX / CX.

The Cadence-supported way to set Spectre LX (vs the default APS) is
**not** via ``+lx`` flag, **not** via ``spectre +preset=lx`` in the
``command`` env option.  Both are silently ignored — the simulation
falls back to APS and you only notice when runtime / accuracy is wrong.

The actual API is two ``asiSetHighPerformanceOptionVal`` calls:

    asiSetHighPerformanceOptionVal(testHandle 'uniMode "Spectre X")
    asiSetHighPerformanceOptionVal(testHandle 'spectreXPreset "LX")

``'uniMode`` accepts: ``"Spectre"``, ``"APS"``, ``"Spectre X"``,
``"Spectre FX"``.  When ``'uniMode`` is ``"Spectre X"``, ``'spectreXPreset``
selects the preset: ``LX`` / ``MX`` / ``AX`` / ``VX`` / ``CX``.

Verify the change took: ``maeGetCurrentNetlistOptionsValues`` should
report ``+preset=lx`` (or whichever) in the resulting Spectre command
line.

Usage::

    python 08_set_simulator_mode.py <LIB> <CELL> <TEST> [<MODE>]

    MODE ∈ {SPECTRE, APS, LX, MX, AX, VX, CX}     (default: LX)

The cell must already have a maestro view with the named test inside.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro.lifecycle import (
    open_session,
    close_session,
)


# Map user-friendly mode names → (uniMode, spectreXPreset-or-None).
# None preset means we leave 'spectreXPreset alone (or set to nil to
# clear it when leaving Spectre X).
MODE_TABLE: dict[str, tuple[str, str | None]] = {
    "SPECTRE":  ("Spectre",    None),
    "APS":      ("APS",        None),
    "LX":       ("Spectre X",  "LX"),
    "MX":       ("Spectre X",  "MX"),
    "AX":       ("Spectre X",  "AX"),
    "VX":       ("Spectre X",  "VX"),
    "CX":       ("Spectre X",  "CX"),
    "FX":       ("Spectre FX", None),
}


def set_simulator_mode(
    client: VirtuosoClient, session: str, test: str, mode: str
) -> None:
    """Apply ``mode`` to ``test`` inside Maestro ``session``.

    ``mode`` is one of MODE_TABLE's keys.  Raises KeyError otherwise.
    """
    uni_mode, preset = MODE_TABLE[mode.upper()]

    # asiGetTest returns the test handle that the SetHighPerformance
    # calls operate on.  Build a local let() so we get one round trip.
    skill = (
        'let((th) '
        f'th = asiGetTest("{test}" "{session}") '
        f'asiSetHighPerformanceOptionVal(th \'uniMode "{uni_mode}") '
    )
    if preset is not None:
        skill += f'asiSetHighPerformanceOptionVal(th \'spectreXPreset "{preset}") '
    skill += ")"

    r = client.execute_skill(skill, timeout=30)
    if r.errors:
        raise RuntimeError(f"set_simulator_mode failed: {r.errors[0]}")


def verify_mode(client: VirtuosoClient, session: str, test: str) -> str:
    """Return the resolved Spectre command line so caller can sanity-check."""
    r = client.execute_skill(
        f'maeGetCurrentNetlistOptionsValues(?session "{session}" ?test "{test}")',
        timeout=30,
    )
    return (r.output or "").strip()


def main() -> int:
    if len(sys.argv) < 4:
        print(
            f"Usage: python {Path(__file__).name} <LIB> <CELL> <TEST> [<MODE>]\n"
            f"  MODE ∈ {{SPECTRE, APS, LX, MX, AX, VX, CX, FX}} (default: LX)\n"
            f"Example: python {Path(__file__).name} PLAYGROUND_LLM TB_FOO tran LX",
            file=sys.stderr,
        )
        return 1

    lib, cell, test = sys.argv[1], sys.argv[2], sys.argv[3]
    mode = sys.argv[4] if len(sys.argv) >= 5 else "LX"

    if mode.upper() not in MODE_TABLE:
        print(f"error: unknown mode {mode!r}; valid: {sorted(MODE_TABLE)}",
              file=sys.stderr)
        return 1

    client = VirtuosoClient.from_env()
    print(f"[1/3] open background session for {lib}/{cell}")
    session = open_session(client, lib, cell)

    print(f"[2/3] set_simulator_mode({test=}, {mode=})")
    set_simulator_mode(client, session, test, mode)

    print(f"[3/3] verify resolved netlist options:")
    print("       " + verify_mode(client, session, test))

    close_session(client, session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

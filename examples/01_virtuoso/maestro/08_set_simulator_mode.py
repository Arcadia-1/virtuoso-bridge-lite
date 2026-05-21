#!/usr/bin/env python3
"""Switch a Maestro test from APS (default) to Spectre X / LX / MX / AX / VX / CX.

The Cadence-supported way to set Spectre LX (vs the default APS) is
**not** via ``+lx`` flag, **not** via ``spectre +preset=lx`` in the
``command`` env option.  Both are silently ignored — the simulation
falls back to APS and you only notice when runtime / accuracy is wrong.

The actual API is two ``asiSetHighPerformanceOptionVal`` calls on the
test's ASI session handle::

    asiSetHighPerformanceOptionVal(testHandle 'uniMode "Spectre X")
    asiSetHighPerformanceOptionVal(testHandle 'spectreXPreset "LX")

``'uniMode`` accepts: ``"Spectre"``, ``"APS"``, ``"Spectre X"``,
``"Spectre FX"``.  When ``'uniMode`` is ``"Spectre X"``, ``'spectreXPreset``
selects the preset: ``LX`` / ``MX`` / ``AX`` / ``VX`` / ``CX``.

Two release-portability notes:

* The test's ASI handle is obtained via ``maeGetTestSession("<test>"
  ?session "<sess>")``.  Older examples used ``asiGetTest`` but it's
  not present in every IC release; ``maeGetTestSession`` is the
  maestro-native equivalent and works in both bg and GUI sessions.
* The setting only persists if the session is saved (``maeSaveSetup``)
  before close.  Without the save, ``uniMode`` reverts to the default
  on the next open, even though ``spectreXPreset`` may appear sticky
  through a different code path.

Verification reads ``uniMode`` and ``spectreXPreset`` back via
``asiGetHighPerformanceOptionVal``.  ``maeGetCurrentNetlistOptionsValues``
is also exposed but only returns the resolved Spectre command line once
the test has been netlisted — typically empty for a fresh bg session.

Usage::

    python 08_set_simulator_mode.py <LIB> <CELL> <TEST> [<MODE>]

    MODE ∈ {SPECTRE, APS, LX, MX, AX, VX, CX, FX}     (default: LX)

The cell must already have a maestro view with the named test inside.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import (
    open_session,
    close_session,
    save_setup,
)


# Map user-friendly mode names → (uniMode, spectreXPreset-or-None).
# preset=None means we don't touch 'spectreXPreset (it's irrelevant
# outside Spectre X mode).
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

    The caller must invoke ``save_setup`` afterwards for the change to
    persist past session close — see module docstring.
    """
    if mode.upper() not in MODE_TABLE:
        raise ValueError(
            f"unknown mode {mode!r}; valid: {sorted(MODE_TABLE)}")
    uni_mode, preset = MODE_TABLE[mode.upper()]

    # One round trip: fetch the ASI test handle, then apply uniMode
    # (and optionally spectreXPreset) on it.  errset() turns a missing
    # handle into a clean failure mode we can detect via th == nil.
    skill = (
        'let((th) '
        f'th = maeGetTestSession("{test}" ?session "{session}") '
        f'unless(th error("maeGetTestSession returned nil for test={test!r} '
        f'session={session!r} — check the test exists in this maestro view")) '
        f'asiSetHighPerformanceOptionVal(th \'uniMode "{uni_mode}") '
    )
    if preset is not None:
        skill += f'asiSetHighPerformanceOptionVal(th \'spectreXPreset "{preset}") '
    skill += ")"

    r = client.execute_skill(skill, timeout=30)
    if r.errors:
        raise RuntimeError(f"set_simulator_mode failed: {r.errors[0]}")


def read_simulator_mode(
    client: VirtuosoClient, session: str, test: str
) -> tuple[str, str | None]:
    """Read back ``(uniMode, spectreXPreset)`` for ``test``.

    ``spectreXPreset`` is ``None`` when not in Spectre X mode (or when
    the option was never set on this test).
    """
    skill = (
        'let((th) '
        f'th = maeGetTestSession("{test}" ?session "{session}") '
        f"list(asiGetHighPerformanceOptionVal(th 'uniMode) "
        f"asiGetHighPerformanceOptionVal(th 'spectreXPreset)))"
    )
    r = client.execute_skill(skill, timeout=30)
    if r.errors:
        raise RuntimeError(f"read_simulator_mode failed: {r.errors[0]}")
    # Parse SKILL list output: ("Spectre X" "MX")  or  ("APS" nil)
    import re
    raw = (r.output or "").strip()
    m = re.match(r'\(\s*"([^"]*)"\s+(.*)\)\s*$', raw)
    if not m:
        raise RuntimeError(f"unexpected output from read_simulator_mode: {raw!r}")
    uni = m.group(1)
    tail = m.group(2).strip()
    preset: str | None
    if tail == "nil":
        preset = None
    else:
        pm = re.match(r'"([^"]*)"', tail)
        preset = pm.group(1) if pm else None
    return uni, preset


def _assert_mode_applied(
    actual: tuple[str, str | None], requested: str
) -> None:
    """Raise if the persisted mode doesn't match what the user asked for."""
    expected_uni, expected_preset = MODE_TABLE[requested.upper()]
    actual_uni, actual_preset = actual
    if actual_uni != expected_uni:
        raise RuntimeError(
            f"uniMode mismatch: expected {expected_uni!r}, got {actual_uni!r}")
    if expected_preset is not None and actual_preset != expected_preset:
        raise RuntimeError(
            f"spectreXPreset mismatch: expected {expected_preset!r}, "
            f"got {actual_preset!r}")


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
    print(f"[1/4] open background session for {lib}/{cell}")
    session = open_session(client, lib, cell)

    try:
        print(f"[2/4] set_simulator_mode({test=}, {mode=})")
        set_simulator_mode(client, session, test, mode)

        print(f"[3/4] save setup (persist uniMode/spectreXPreset)")
        save_setup(client, lib, cell, session=session)

        print(f"[4/4] verify persisted mode:")
        uni, preset = read_simulator_mode(client, session, test)
        print(f"       uniMode={uni!r}, spectreXPreset={preset!r}")
        _assert_mode_applied((uni, preset), mode)
        print(f"       OK — mode {mode.upper()} applied")
    finally:
        close_session(client, session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

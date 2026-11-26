"""Window-state probes for the focused maestro session.

Helpers used by ``snapshot()`` and the CLI brief:

* ``_fetch_window_state`` — 1 SKILL call → focused title + davSession
  + window list + session list, title parsed into lib/cell/view/mode.
* ``natural_sort_histories`` — filter a results/maestro listing for
  history anchors and natural-sort by name.  Fragile when history
  names mix numbered + custom prefixes; keep as last-resort fallback.
* ``sort_histories_by_mtime`` — preferred ordering: sort histories by
  their newest on-disk mtime.  Matches the "what did I last run"
  intuition even when names mix Interactive.N / ExplorerRun.N /
  custom strings.
"""

from __future__ import annotations

import re

from virtuoso_bridge import VirtuosoClient

from .state import get_session_state, parse_maestro_title


# A history is anchored by its .rdb metadata file (any user-given name —
# Interactive.0.RO, closeloop_PVT_postsim, sweep_set.3, etc.).  Bare
# directories matching Cadence's ``Interactive.N`` / ``MonteCarlo.N``
# shape are also accepted for setups without .rdb anchors.
_HISTORY_RDB_RE = re.compile(r"^(?!\.)[^/\\]+\.rdb$")
_HISTORY_COMPANION_RE = re.compile(r"^(?!\.)([^/\\]+)\.(?:log|msg\.db)$")
_HISTORY_DIR_RE = re.compile(r"^(Interactive|MonteCarlo)\.[0-9]+(?:\.[A-Z]{2,4})?$")


def _parse_mae_title(titles) -> dict:
    """Return parsed fields from the first maestro-shaped title, or
    ``{}`` if none match.

    Title shape: ``ADE {Assembler|Explorer} {Editing|Reading}: LIB CELL VIEW[*]``
    (trailing ``*`` = unsaved changes).  Fields:
    ``application / lib / cell / view / mode / unsaved``.
    """
    for n in titles or ():
        if not n:
            continue
        parsed = parse_maestro_title(n)
        if parsed is None:
            continue
        return {
            "application": parsed["application"],
            "lib": parsed["lib"], "cell": parsed["cell"], "view": parsed["view"],
            "mode": str(parsed["access"]).title(),
            "unsaved": parsed["unsaved"],
        }
    return {}


def _fetch_window_state(client: VirtuosoClient) -> dict:
    """One SKILL round-trip → focused window info, title parsed.

    Keys: ``session`` (davSession — ``""`` if focus isn't a maestro
    window), ``title``, a structured ``state``, plus the parsed fields
    from the focused title: ``application / lib / cell /
    view / mode / unsaved`` (empty / None when nothing parsed).

    davSession is Cadence's own attribute for the bound maestro
    session on ADE Assembler windows — avoids sdb-scp disambiguation.
    """
    state = get_session_state(client)
    title = state.title or ""
    return {
        "session":      state.session or "",
        "title":        title,
        "application":  state.application,
        "lib":          state.lib or "",
        "cell":         state.cell or "",
        "view":         state.view or "",
        "mode":         state.access.title() if state.access != "unknown" else "Unknown",
        "unsaved":      state.unsaved,
        "state":        state.model_dump(mode="json"),
    }


def _history_name_for_file(fname: str) -> str | None:
    """Return the history name a file belongs to, or None if it's not a
    history artifact.  Recognises ``<name>.rdb`` anchors, companion
    ``.log`` / ``.msg.db`` files (excluding dotfiles and path separators),
    and bare ``Interactive.N`` / ``MonteCarlo.N`` directories."""
    if _HISTORY_RDB_RE.match(fname):
        return fname[:-4]   # strip .rdb
    companion = _HISTORY_COMPANION_RE.match(fname)
    if companion:
        return companion.group(1)
    if _HISTORY_DIR_RE.match(fname):
        return fname
    return None


def natural_sort_histories(hist_files: list[str]) -> list[str]:
    """Extract + naturally-sort history names from a results/maestro listing.

    Anchors on ``<name>.rdb``; also accepts bare ``Interactive.N`` /
    ``MonteCarlo.N`` dirs.  ``Interactive.2`` sorts before
    ``Interactive.10``.  Pure function.
    """
    seen: set[str] = set()
    for h in hist_files:
        if _HISTORY_RDB_RE.match(h):
            seen.add(h[:-4])
        elif _HISTORY_DIR_RE.match(h):
            seen.add(h)

    def _natkey(s: str):
        return [
            (int(tok) if tok.isdigit() else 0, tok)
            for tok in re.findall(r"\d+|\D+", s)
        ]

    return sorted(seen, key=_natkey)


def sort_histories_by_mtime(
    hist_files_mtime: list[tuple[str, int]],
) -> list[str]:
    """Extract history names from a ``[(filename, mtime), ...]`` listing
    and sort by newest mtime first.

    Valid histories are anchored strictly by ``<name>.rdb`` files so
    orphan logs (such as ``Job0.log``) and dotfiles (such as ``.log``)
    are never promoted as histories.  Matching companion files
    (``<name>.log``, ``<name>.msg.db``) update the timestamp only for
    already-known ``.rdb`` histories, taking the maximum mtime across
    all files belonging to that history.
    """
    bucket: dict[str, int] = {}
    for fname, mtime in hist_files_mtime:
        if _HISTORY_RDB_RE.match(fname):
            hist = fname[:-4]
            if hist not in bucket or mtime > bucket[hist]:
                bucket[hist] = mtime
    for fname, mtime in hist_files_mtime:
        companion = _HISTORY_COMPANION_RE.match(fname)
        if companion:
            hist = companion.group(1)
            if hist in bucket and mtime > bucket[hist]:
                bucket[hist] = mtime
    return sorted(bucket, key=lambda h: bucket[h], reverse=True)

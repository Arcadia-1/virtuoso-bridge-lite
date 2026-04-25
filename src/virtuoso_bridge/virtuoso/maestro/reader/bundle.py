"""SKILL probes for ``snapshot()`` — each probe is its own label.

Two round-trips total:

1. **Discover** — single SKILL call → ``test`` name + ``enabled``
   analysis names.  Needed because per-analysis probes reference these
   in their text (so the probe string is also its label).

2. **Batch** — single SKILL ``list(...)`` call running every probe
   independently (no shared ``let``-bindings).  Each probe string is
   self-contained Cadence SKILL — no ``car(libPath)`` style internal
   references.  The string is what we ran *and* what we display as
   the section header.

Returns ``raw_sections`` — list of ``(probe_skill_text, raw_output)``
tuples — plus a few convenience fields (``test``, ``hist_files``)
that ``snapshot()`` needs for path derivation.  No SKILL alist→Python
dict parsing.
"""

from __future__ import annotations

from virtuoso_bridge import VirtuosoClient

from ._parse_skill import _parse_skill_str_list, _tokenize_top_level


# ---------------------------------------------------------------------------
# Shared helpers — SKILL output text wrangling
# ---------------------------------------------------------------------------

def _split_top_level(raw: str, expected: int) -> list[str]:
    """Strip outer parens, tokenize top-level into ``expected`` slots,
    pad with empty strings if the response was truncated."""
    body = (raw or "").strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    slots = _tokenize_top_level(
        body,
        include_strings=True, include_atoms=True, include_groups=True,
        max_tokens=expected,
    )
    while len(slots) < expected:
        slots.append("")
    return slots


def _unwrap_errset(s: str) -> str:
    """``errset(X)`` returns ``(X)`` on success or ``nil`` on error.
    Strip the outer parens (or return "" on error)."""
    s = (s or "").strip()
    if s in ("", "nil"):
        return ""
    if s.startswith("(") and s.endswith(")"):
        return s[1:-1].strip()
    return s


def brief_bundle(client: VirtuosoClient, *,
                 sess: str, lib: str, cell: str, view: str) -> dict:
    """One SKILL round-trip → just the 4 probes the CLI brief shows.

    No env_options / sim_options / outputs / dir-listing / status —
    those go in ``full_bundle`` for the disk dump.  Brief deliberately
    only fetches what it prints (lib readPath, test name, enabled
    analyses, per-analysis settings).

    Returns ``{"raw_sections": [(label, raw_text), ...]}`` — same shape
    as ``full_bundle``, just smaller.
    """
    if not sess:
        return {"raw_sections": []}
    expr = f'''
list(
  ddGetObj("{lib}")~>readPath
  maeGetSetup(?session "{sess}")
  maeGetEnabledAnalysis(car(maeGetSetup(?session "{sess}")) ?session "{sess}")
  mapcar(lambda((a) maeGetAnalysis(car(maeGetSetup(?session "{sess}")) a ?session "{sess}"))
         maeGetEnabledAnalysis(car(maeGetSetup(?session "{sess}")) ?session "{sess}"))
)
'''
    r = client.execute_skill(expr)
    s_lib, s_setup, s_enabled, s_analyses = _split_top_level(r.output or "", expected=4)

    tests = _parse_skill_str_list(_unwrap_errset(s_setup))
    test = tests[0] if tests else ""
    enabled = _parse_skill_str_list(_unwrap_errset(s_enabled))

    sections = [
        (f'ddGetObj("{lib}")~>readPath',                          s_lib),
        (f'maeGetSetup(?session "{sess}")',                       s_setup),
        (f'maeGetEnabledAnalysis("{test}" ?session "{sess}")',    s_enabled),
    ]
    per_ana = _split_top_level(s_analyses, expected=len(enabled)) if enabled else []
    for ana, raw in zip(enabled, per_ana):
        sections.append(
            (f'maeGetAnalysis("{test}" "{ana}" ?session "{sess}")', raw))
    return {"raw_sections": sections}


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
#
# Each entry below is a self-contained SKILL expression — no internal
# let-var references.  Templates use ``{sess}`` / ``{lib}`` / ``{cell}``
# / ``{view}`` / ``{test}`` placeholders, .format()-substituted at run
# time so the formatted string IS the section label printed to the user.
#
# Per-analysis maeGetAnalysis calls are generated dynamically from the
# enabled list discovered in the first round-trip.

_PROBES_TEMPLATE: tuple[str, ...] = (
    'ddGetObj("{lib}")~>readPath',
    'maeGetSetup(?session "{sess}")',
    'maeGetEnabledAnalysis("{test}" ?session "{sess}")',
    # per-analysis maeGetAnalysis(...) probes inserted here at run-time
    'maeGetEnvOption("{test}" ?session "{sess}")',
    'maeGetSimOption("{test}" ?session "{sess}")',
    'mapcar(lambda((o) list(o~>name o~>type o~>signal o~>expression'
        ' o~>plot o~>save o~>evalType o~>yaxisUnit o~>spec))'
        ' maeGetTestOutputs("{test}" ?session "{sess}"))',
    'maeGetCurrentRunMode(?session "{sess}")',
    'maeGetJobControlMode(?session "{sess}")',
    'errset(maeGetRunPlan(?session "{sess}"))',
    # Currently-loaded history name (ground truth for "which history is
    # the user looking at").  IC6.1.8 returns a runHistory object whose
    # ~>name is often nil; try a couple of alternative accessors and
    # surface whichever yields a non-nil string.
    'errset(let((h) h=axlGetCurrentHistory("{sess}") '
        'when(h list(h~>name h~>historyName h~>run h~>runName))))',
    'errset(maeGetSimulationMessages(?session "{sess}" ?msgType "error"))',
    'errset(maeGetSimulationMessages(?session "{sess}" ?msgType "warning"))',
    'errset(maeGetSimulationMessages(?session "{sess}" ?msgType "info"))',
    # Plain file listing (works on every IC version). Mtime-based
    # ordering is filled in by a separate shell call in bundle.py
    # because SKILL's getFileWriteTime is unavailable in some builds.
    'getDirFiles(strcat(ddGetObj("{lib}")~>readPath "/{cell}/{view}/results/maestro"))',
    'errset(asiGetAnalogRunDir(asiGetSession("{sess}")))',
)


def full_bundle(client: VirtuosoClient, *,
                sess: str, lib: str, cell: str, view: str) -> dict:
    """Two SKILL round-trips → ``raw_sections`` + path-derivation hints.

    Returns::

        {"raw_sections":       [(probe_skill_text, raw_output), ...],
         "test":               str,   # car(maeGetSetup) — for path derivation
         "current_history":    str,   # axlGetCurrentHistory~>name, "" if none
         "hist_files":         [str, ...],                # filenames only
         "hist_files_mtime":   [(fname, unix_mtime), ...] # same list + mtimes
        }

    No alist→dict parsing.  The probe strings ARE the section labels;
    callers print ``raw_sections`` verbatim.
    """
    if not sess:
        return {"raw_sections": [], "test": "", "current_history": "",
                "hist_files": [], "hist_files_mtime": []}

    # --- Round 1: discover the test name + enabled analyses ---
    # Both are needed to format the per-analysis probes in round 2 (so
    # their printed labels show the actual test/analysis names).
    r = client.execute_skill(
        f'list('
        f'maeGetSetup(?session "{sess}") '
        f'maeGetEnabledAnalysis(car(maeGetSetup(?session "{sess}")) ?session "{sess}"))'
    )
    d = _split_top_level(r.output or "", expected=2)
    tests = _parse_skill_str_list(_unwrap_errset(d[0]))
    test = tests[0] if tests else ""
    enabled = _parse_skill_str_list(_unwrap_errset(d[1]))

    # --- Round 2: every probe, formatted into self-contained SKILL ---
    fmt = {"sess": sess, "lib": lib, "cell": cell, "view": view, "test": test}
    # Insert per-analysis probes after maeGetEnabledAnalysis (index 3).
    head = [p.format(**fmt) for p in _PROBES_TEMPLATE[:3]]
    per_ana = [
        f'maeGetAnalysis("{test}" "{ana}" ?session "{sess}")'
        for ana in enabled
    ]
    tail = [p.format(**fmt) for p in _PROBES_TEMPLATE[3:]]
    probes = head + per_ana + tail

    list_body = "\n  ".join(probes)
    r2 = client.execute_skill(f'list(\n  {list_body}\n)')
    outputs = _split_top_level(r2.output or "", expected=len(probes))

    # Convenience fields snapshot()'s disk-dump path computation needs.
    # All extracted from the same raw_sections (no extra SKILL).
    lib_path = ""
    scratch_root = ""
    current_history = ""
    hist_files: list[str] = []
    for label, raw in zip(probes, outputs):
        if label.startswith('ddGetObj('):
            lib_path = (raw or "").strip().strip('"')
        elif label.startswith('errset(let((h) h=axlGetCurrentHistory'):
            # Probe returned list(~>name ~>historyName ~>run ~>runName);
            # pick the first non-nil string value.
            current_history = _first_nonnil_string(_unwrap_errset(raw))
        elif label.startswith("getDirFiles("):
            hist_files = _parse_skill_str_list(_unwrap_errset(raw))
        elif label.startswith("errset(asiGetAnalogRunDir"):
            run_dir = _unwrap_errset(raw).strip().strip('"')
            marker = f"/{lib}/{cell}/{view}/results/maestro"
            idx = run_dir.find(marker)
            if idx > 0:
                scratch_root = run_dir[:idx]

    # mtime-augmented listing via a single shell call (SKILL's
    # getFileWriteTime isn't universally available).  Always collected
    # now — snapshot() prefers mtime over current_history, and with
    # persistent SSH shell this extra trip is ~20 ms.  Skipping it
    # caused snapshot to follow a stale GUI-loaded history
    # (e.g. ExplorerRun.0) instead of the actual newest run.
    #
    # We scan *two* locations and merge:
    #   project maestro  — the authoritative history DB for runs that
    #                      checkpoint back (regular Interactive.N, etc.)
    #   scratch_root maestro — where Explorer-derived `.RO` runs and
    #                          any run that stays local-only materialize.
    # Without the scratch side, `.RO` histories (which live only there)
    # are invisible to snapshot() and it falls back to the stale
    # project-side mtime champion.
    hist_files_mtime: list[tuple[str, int]] = []
    if lib_path and hist_files:
        hist_files_mtime = _fetch_mtimes_via_shell(
            client, f"{lib_path}/{cell}/{view}/results/maestro",
        )
    if scratch_root:
        scratch_maestro = f"{scratch_root}/{lib}/{cell}/{view}/results/maestro"
        scratch_mtimes = _fetch_mtimes_via_shell(client, scratch_maestro)
        seen = {f for f, _ in hist_files_mtime}
        for f, m in scratch_mtimes:
            if f not in seen:
                hist_files_mtime.append((f, m))
                seen.add(f)

    return {
        "raw_sections":     list(zip(probes, outputs)),
        "test":             test,
        "current_history":  current_history,
        "hist_files":       hist_files,
        "hist_files_mtime": hist_files_mtime,
        "lib_path":         lib_path,
        "scratch_root":     scratch_root,
    }


def _first_nonnil_string(val: str) -> str:
    """From a SKILL list like ``("x" nil nil "y")`` (or ``nil`` / ``""``),
    return the first non-nil quoted string value, else ``""``."""
    import re as _re
    m = _re.findall(r'"([^"]*)"', val or "")
    for s in m:
        if s:
            return s
    return ""


def _fetch_mtimes_via_shell(client: VirtuosoClient, remote_dir: str,
                             ) -> list[tuple[str, int]]:
    """``find -printf`` → ``[(basename, unix_mtime), ...]`` via one
    ssh round-trip.  Returns ``[]`` on any failure (caller falls back
    to natural-sort-by-name)."""
    import re as _re
    runner = getattr(getattr(client, "_tunnel", None), "_ssh_runner", None)
    if runner is None:
        return []
    # %T@ prints unix mtime as a float; %f is basename. '\n' between
    # rows, ' ' between fields.
    cmd = (f"find {remote_dir} -maxdepth 1 -type f "
           f"-printf '%T@ %f\\n' 2>/dev/null")
    try:
        r = runner.run_command(cmd, timeout=15)
    except Exception:
        return []
    out = (r.stdout or "") if hasattr(r, "stdout") else ""
    pairs: list[tuple[str, int]] = []
    for line in out.splitlines():
        m = _re.match(r"^(\d+)(?:\.\d+)?\s+(.+)$", line.strip())
        if m:
            pairs.append((m.group(2), int(m.group(1))))
    return pairs

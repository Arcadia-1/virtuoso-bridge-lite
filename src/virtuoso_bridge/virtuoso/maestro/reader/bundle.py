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

import shlex

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.ops import escape_skill_string

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
    esess = escape_skill_string(sess)
    elib = escape_skill_string(lib)
    expr = f'''
let((vbSetup vbTest vbEnabled)
  vbSetup = car(errset(maeGetSetup(?session "{esess}")))
  vbTest = when(listp(vbSetup) car(vbSetup))
  vbEnabled = when(stringp(vbTest) car(errset(maeGetEnabledAnalysis(vbTest ?session "{esess}"))))
  list(
    car(errset(ddGetObj("{elib}")~>readPath))
    vbSetup
    vbEnabled
    when(stringp(vbTest) && listp(vbEnabled)
      mapcar(lambda((a) car(errset(maeGetAnalysis(vbTest a ?session "{esess}")))) vbEnabled))
  )
)
'''
    r = client.execute_skill(expr)
    s_lib, s_setup, s_enabled, s_analyses = _split_top_level(r.output or "", expected=4)

    tests = _parse_skill_str_list(_unwrap_errset(s_setup))
    test = tests[0] if tests else ""
    etest = escape_skill_string(test)
    enabled = _parse_skill_str_list(_unwrap_errset(s_enabled))

    sections = [
        (f'ddGetObj("{elib}")~>readPath',    s_lib),
        (f'maeGetSetup(?session "{esess}")', s_setup),
    ]
    if test:
        sections.append(
            (f'maeGetEnabledAnalysis("{etest}" ?session "{esess}")', s_enabled),
        )
        per_ana = _split_top_level(s_analyses, expected=len(enabled)) if enabled else []
        for ana, raw in zip(enabled, per_ana):
            eana = escape_skill_string(ana)
            sections.append(
                (f'maeGetAnalysis("{etest}" "{eana}" ?session "{esess}")', raw))
    return {"raw_sections": sections}


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
#
# Each entry below is a self-contained SKILL expression — no internal
# let-var references.  Templates use ``{sess}`` / ``{lib}`` / ``{cell}``
# / ``{view}`` / ``{test}`` placeholders, .format()-substituted at run
# time with escaped SKILL strings so the formatted string IS the section
# label printed to the user.
#
# Per-analysis maeGetAnalysis calls are generated dynamically from the
# enabled list discovered in the first round-trip.

_HEAD_PROBES_TEMPLATE: tuple[str, ...] = (
    'car(errset(ddGetObj("{lib}")~>readPath))',
    'errset(maeGetSetup(?session "{sess}"))',
)

_TEST_ENV_PROBES_TEMPLATE: tuple[str, ...] = (
    'errset(maeGetEnvOption("{test}" ?session "{sess}"))',
    'errset(maeGetSimOption("{test}" ?session "{sess}"))',
    'errset(mapcar(lambda((o) list(o~>name o~>type o~>signal o~>expression'
        ' o~>plot o~>save o~>evalType o~>yaxisUnit o~>spec))'
        ' maeGetTestOutputs("{test}" ?session "{sess}")))',
)

_TAIL_PROBES_TEMPLATE: tuple[str, ...] = (
    'errset(maeGetCurrentRunMode(?session "{sess}"))',
    'errset(maeGetJobControlMode(?session "{sess}"))',
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
    'errset(let((p) p=ddGetObj("{lib}")~>readPath '
        'when(stringp(p) getDirFiles(strcat(p "/{cell}/{view}/results/maestro")))))',
    'errset(asiGetAnalogRunDir(asiGetSession("{sess}")))',
)

_PROBES_TEMPLATE: tuple[str, ...] = (
    _HEAD_PROBES_TEMPLATE
    + ('errset(maeGetEnabledAnalysis("{test}" ?session "{sess}"))',)
    + _TEST_ENV_PROBES_TEMPLATE
    + _TAIL_PROBES_TEMPLATE
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

    esess = escape_skill_string(sess)
    elib = escape_skill_string(lib)
    ecell = escape_skill_string(cell)
    eview = escape_skill_string(view)

    # --- Round 1: discover the test name + enabled analyses ---
    # Guard maeGetSetup so empty / newly created Maestro views do not
    # abort subsequent probes.
    r = client.execute_skill(
        'let((vbSetup vbTest vbEnabled) '
        f'vbSetup = car(errset(maeGetSetup(?session "{esess}"))) '
        'vbTest = when(listp(vbSetup) car(vbSetup)) '
        f'vbEnabled = when(stringp(vbTest) car(errset(maeGetEnabledAnalysis(vbTest ?session "{esess}")))) '
        'list(vbSetup vbEnabled))'
    )
    d = _split_top_level(r.output or "", expected=2)
    tests = _parse_skill_str_list(_unwrap_errset(d[0]))
    test = tests[0] if tests else ""
    etest = escape_skill_string(test)
    enabled = _parse_skill_str_list(_unwrap_errset(d[1])) if test else []

    # --- Round 2: every probe, formatted into self-contained SKILL ---
    fmt = {
        "sess": esess,
        "lib": elib,
        "cell": ecell,
        "view": eview,
        "test": etest,
    }
    probes = [p.format(**fmt) for p in _HEAD_PROBES_TEMPLATE]
    if test:
        probes.append(f'errset(maeGetEnabledAnalysis("{etest}" ?session "{esess}"))')
        probes.extend(
            f'errset(maeGetAnalysis("{etest}" "{escape_skill_string(ana)}" ?session "{esess}"))'
            for ana in enabled
        )
        probes.extend(p.format(**fmt) for p in _TEST_ENV_PROBES_TEMPLATE)
    probes.extend(p.format(**fmt) for p in _TAIL_PROBES_TEMPLATE)

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
        if "ddGetObj(" in label and "getDirFiles(" not in label:
            val = (raw or "").strip().strip('"')
            lib_path = "" if val == "nil" else val
        elif label.startswith('errset(let((h) h=axlGetCurrentHistory'):
            # Probe returned list(~>name ~>historyName ~>run ~>runName);
            # pick the first non-nil string value.
            current_history = _first_nonnil_string(_unwrap_errset(raw))
        elif "getDirFiles(" in label:
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
    # We scan *two* locations and merge, keeping the newest mtime per
    # filename so newer scratch_root timestamps are preserved alongside
    # project-side entries:
    #   project maestro  — the authoritative history DB for runs that
    #                      checkpoint back (regular Interactive.N, etc.)
    #   scratch_root maestro — where Explorer-derived `.RO` runs and
    #                          any run that stays local-only materialize.
    merged_mtimes: dict[str, int] = {}
    if lib_path and hist_files:
        for f, m in _fetch_mtimes_via_shell(
            client, f"{lib_path}/{cell}/{view}/results/maestro",
        ):
            if f not in merged_mtimes or m > merged_mtimes[f]:
                merged_mtimes[f] = m
    if scratch_root:
        scratch_maestro = f"{scratch_root}/{lib}/{cell}/{view}/results/maestro"
        for f, m in _fetch_mtimes_via_shell(client, scratch_maestro):
            if f not in merged_mtimes or m > merged_mtimes[f]:
                merged_mtimes[f] = m
    hist_files_mtime = list(merged_mtimes.items())

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
    """``find -printf`` (remote) or ``Path.stat`` (local) →
    ``[(basename, unix_mtime), ...]``.  Returns ``[]`` on any failure
    (caller falls back to natural-sort-by-name)."""
    import re as _re
    runner = getattr(client, "ssh_runner", None)
    if runner is None:
        runner = getattr(getattr(client, "_tunnel", None), "_ssh_runner", None)
    if runner is None:
        # Local mode: Virtuoso shares the filesystem; iterate directly.
        from pathlib import Path as _Path
        try:
            d = _Path(remote_dir)
            if not d.is_dir():
                return []
            return [(p.name, int(p.stat().st_mtime))
                    for p in d.iterdir() if p.is_file()]
        except OSError:
            return []
    # %T@ prints unix mtime as a float; %f is basename. '\n' between
    # rows, ' ' between fields.
    cmd = (f"find {shlex.quote(remote_dir)} -maxdepth 1 -type f "
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

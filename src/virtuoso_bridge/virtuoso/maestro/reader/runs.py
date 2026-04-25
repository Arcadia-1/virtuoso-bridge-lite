"""Post-simulation result reads + OCEAN waveform export.

Two public entry points:

- :func:`read_results` — export Cadence's "Detail" results CSV
  (per-point × per-output table) for the latest valid history,
  parse it into a per-point structure.  This is *consumption-time*
  data (numbers users compute on), so the return is a dict —
  distinct from snapshot's "describe the setup" flow which keeps
  SKILL outputs as raw text.
- :func:`export_waveform` — call OCEAN's ``ocnPrint`` to dump one
  expression's waveform to a local text file via scp.
"""

from __future__ import annotations

import csv
import re
import tempfile
import uuid
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

from ._parse_skill import _parse_skill_str_list
from ._skill import _q, _get_test, _unique_remote_wave_path
from .session import natural_sort_histories


# ---------------------------------------------------------------------------
# read_results — GUI-mode scalar / spec-status reads
# ---------------------------------------------------------------------------

def read_results(client: VirtuosoClient, session: str,
                  lib: str = "", cell: str = "",
                  history: str = "",
                  *,
                  include_raw: bool = False) -> dict:
    """Read simulation results: per-point × per-output values + specs.

    Uses Cadence's ``maeExportOutputView ?view "Detail"`` to dump the
    full results table to CSV, scp's it back, and parses into a
    per-point structure.  This is the canonical "all points × all
    outputs" view — vs ``maeGetOutputValue`` which only gives the
    currently-selected point and ``.log`` which shows only the
    "best" point.

    Requires GUI mode (Cadence's expression evaluator must be live).
    Finds the latest valid history if ``history=`` not given.

    Returns::

        {
          "history": "Interactive.7",
          "tests":   [test_name, ...],
          "points":  [
            {"point": 1,
             "parameters": {"VDD": "0.9", ...},
             "outputs":    {"Gain_dB": {"value": "21.63",
                                        "spec": "", "weight": "",
                                        "pass_fail": ""}, ...}},
            {"point": 2, ...},
          ],
          "overall_spec":  "passed" | "failed" | None,
          "overall_yield": "(nil Yield 100 PassedPoints 3 ...)" | None,
        }

    For back-compat, ``"outputs": [...]`` is also emitted as the
    flattened (test, name, value, spec_status) list across all points.

    With ``include_raw=True`` the raw exported CSV text is attached
    under ``"raw_csv"`` for debug / audit.

    Args:
        session: active session string
        lib: library name (auto-detected if empty)
        cell: cell name (auto-detected if empty)
        history: explicit history name; otherwise picks the latest
            history that has results.
        include_raw: include raw CSV text under ``"raw_csv"``.
    """
    def q(label, expr):
        return _q(client, label, expr)

    # Auto-detect lib/cell from session env if not provided.
    if not lib or not cell:
        test = _get_test(client, session)
        if test:
            if not lib:
                r = client.execute_skill(
                    f'maeGetEnvOption("{test}" ?option "lib" ?session "{session}")')
                lib = (r.output or "").strip('"')
            if not cell:
                r = client.execute_skill(
                    f'maeGetEnvOption("{test}" ?option "cell" ?session "{session}")')
                cell = (r.output or "").strip('"')
    if not lib or not cell:
        return {}

    test = _get_test(client, session)
    if not test:
        return {}

    # Pick the latest history with actual results (newest-first scan).
    if history:
        latest_history = history.strip()
    else:
        latest_history = _find_latest_history_with_results(
            client, lib=lib, cell=cell, test=test)
    if not latest_history or latest_history == "nil":
        return {}

    # Export the full Detail table to a remote tmp CSV; scp; parse.
    remote_csv = f"/tmp/vb_results_{uuid.uuid4().hex}.csv"
    export_cmd = (
        f'maeExportOutputView('
        f'  ?session "{session}"'
        f'  ?testName "{test}"'
        f'  ?historyName "{latest_history}"'
        f'  ?view "Detail"'
        f'  ?fileName "{remote_csv}"'
        f')'
    )
    r = q("maeExportOutputView", export_cmd)
    if not r or "/tmp/" not in r:
        return {}

    local_csv = Path(tempfile.gettempdir()) / f"vb_results_{uuid.uuid4().hex}.csv"
    csv_text = ""
    try:
        client.download_file(remote_csv, str(local_csv))
        csv_text = local_csv.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    finally:
        try:
            local_csv.unlink()
        except OSError:
            pass
        # Best-effort remote cleanup.
        try:
            client.execute_skill(f'deleteFile("{remote_csv}")')
        except Exception:
            pass

    raw_overall = q("maeGetOverallSpecStatus", 'maeGetOverallSpecStatus()')
    raw_yield   = q("maeGetOverallYield",
                    f'maeGetOverallYield("{latest_history}")')

    structured = _parse_detail_csv(csv_text, history=latest_history)
    structured["overall_spec"]  = _unquote_atom(raw_overall)
    structured["overall_yield"] = _unquote_atom(raw_yield)
    if include_raw:
        structured["raw_csv"] = csv_text
    return structured


def _find_latest_history_with_results(client: VirtuosoClient, *,
                                       lib: str, cell: str, test: str) -> str:
    """Scan the results dir newest-first; return the first history
    that successfully opens with at least one result output.  Returns
    ``""`` when none qualify."""
    r = client.execute_skill(
        f'let((p d) '
        f'p = ddGetObj("{lib}")~>readPath '
        f'd = strcat(p "/{cell}/maestro/results/maestro") '
        f'if(isDir(d) getDirFiles(d) nil))'
    )
    files = _parse_skill_str_list(r.output or "")
    hist_list = natural_sort_histories(files)
    for h in reversed(hist_list):
        r = client.execute_skill(
            f'when(maeOpenResults(?history "{h}") '
            f'  let((outs) '
            f'    outs = maeGetResultOutputs(?testName "{test}") '
            f'    maeCloseResults() '
            f'    outs))'
        )
        out = (r.output or "").strip()
        if out and out != "nil":
            return h
    return ""


# CSV shape from ``maeExportOutputView ?view "Detail"``::
#
#     ,,Parameter,Nominal,,,
#     <blank>
#     Point,Test,Output,Nominal,Spec,Weight,Pass/Fail
#     Parameters: KEY=VAL,,,,,,
#     <point#>,<test>,<output_name_or_expr>,<value>,<spec>,<weight>,<pass_fail>
#     ...   (one block per point)
#
# A "Parameters: ..." row marks the start of each new point.

def _parse_detail_csv(text: str, *, history: str) -> dict:
    """Parse maeExportOutputView Detail CSV → per-point dict.  Pure."""
    points: list[dict] = []
    current: dict | None = None
    tests_seen: set[str] = set()

    reader = csv.reader(text.splitlines())
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        first = (row[0] or "").strip()
        if first.startswith("Parameters:"):
            # Start a new point.  Parse "Parameters: K1=V1, K2=V2, ..."
            params_text = first[len("Parameters:"):].strip()
            params: dict[str, str] = {}
            for kv in params_text.split(","):
                kv = kv.strip()
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    params[k.strip()] = v.strip()
            current = {"point": len(points) + 1,
                       "parameters": params, "outputs": {}}
            points.append(current)
            continue
        # Skip header / non-data rows
        if first in ("", "Point") or not first.isdigit():
            continue
        # Data row: point, test, output, nominal, spec, weight, pass_fail
        if current is None:
            current = {"point": int(first), "parameters": {}, "outputs": {}}
            points.append(current)
        # Cadence sometimes inserts cells with quoted commas; csv module
        # handles the quoting so columns line up.
        cols = row + [""] * (7 - len(row))
        _, test_n, name, value, spec, weight, pass_fail = cols[:7]
        if test_n:
            tests_seen.add(test_n.strip())
        if name.strip():
            current["outputs"][name.strip()] = {
                "value":     value.strip(),
                "spec":      spec.strip(),
                "weight":    weight.strip(),
                "pass_fail": pass_fail.strip(),
            }

    # Back-compat flat list: (test, name, value, spec_status) per point×output.
    flat_outputs: list[dict] = []
    for p in points:
        for name, info in p["outputs"].items():
            flat_outputs.append({
                "point":       p["point"],
                "name":        name,
                "value":       info["value"],
                "spec_status": info["pass_fail"],
            })

    return {
        "history": history,
        "tests":   sorted(tests_seen),
        "points":  points,
        "outputs": flat_outputs,
    }


def _unquote_atom(raw: str) -> str | None:
    s = (raw or "").strip().strip('"')
    if not s or s.lower() == "nil":
        return None
    return s


# ---------------------------------------------------------------------------
# export_waveform — OCEAN-driven single-expression dump
# ---------------------------------------------------------------------------

def export_waveform(
    client: VirtuosoClient,
    session: str,
    expression: str,
    local_path: str,
    *,
    analysis: str = "ac",
    history: str = "",
) -> str:
    """Export a waveform via OCEAN to a local text file.

    Args:
        session: session string (used to find history if not given)
        expression: OCEAN expression, e.g. ``'dB20(mag(VF("/VOUT")))'``
        local_path: where to save locally
        analysis: which analysis to select ("ac", "tran", "noise", etc.)
        history: explicit history name; auto-detected if empty

    Returns the local file path.
    """
    # Auto-detect history name from the current results dir.
    # The path shape is `.../maestro/results/maestro/{history}/...` where
    # `{history}` can be any name Cadence wrote — Interactive.N, sweep_*,
    # ExplorerRun.0, user-named, etc.  We capture any non-slash run.
    if not history:
        r = client.execute_skill('asiGetResultsDir(asiGetCurrentSession())')
        rd = (r.output or "").strip('"')
        m = re.search(r'/maestro/results/maestro/([^/]+)/', rd)
        if m:
            history = m.group(1)
        else:
            raise RuntimeError(
                "No simulation history found from asiGetResultsDir. "
                "Pass history= explicitly, or ensure maestro GUI is open."
            )

    remote_path = _unique_remote_wave_path(history)

    # First maeOpenResults to point asiGetResultsDir at the correct history,
    # then use OCEAN openResults with that path.
    client.execute_skill(f'maeOpenResults(?history "{history}")')
    r = client.execute_skill('asiGetResultsDir(asiGetCurrentSession())')
    results_dir = (r.output or "").strip('"')
    client.execute_skill('maeCloseResults()')

    if not results_dir or results_dir == "nil" or "tmpADE" in results_dir:
        raise RuntimeError(f"No valid results directory for {history}")
    if f"/{history}/" not in results_dir:
        raise RuntimeError(
            f"History mismatch: expected {history}, got resultsDir={results_dir}"
        )

    client.execute_skill(f'openResults("{results_dir}")')
    client.execute_skill(f'selectResults("{analysis}")')
    client.execute_skill(
        f'ocnPrint({expression} '
        f'?numberNotation \'scientific ?numSpaces 1 '
        f'?output "{remote_path}")')

    client.download_file(remote_path, local_path)
    client.execute_skill(f'deleteFile("{remote_path}")')
    return local_path

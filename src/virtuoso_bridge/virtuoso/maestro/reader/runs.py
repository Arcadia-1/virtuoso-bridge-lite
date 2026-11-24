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
import logging
import re
import shlex
import tempfile
import uuid
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

from virtuoso_bridge.virtuoso.maestro.writer import _export_output_view_skill

from ._parse_skill import _parse_skill_str_list
from ._skill import _q, _get_test, _unique_remote_wave_path
from .session import natural_sort_histories

logger = logging.getLogger(__name__)


class NoResults(dict):
    """Empty :func:`read_results` value that says why it is empty.

    Equal to ``{}`` and falsy as before, but ``result["points"]`` raises a
    ``KeyError`` naming the reason instead of a bare ``'points'``.
    """

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason

    def __missing__(self, key):
        raise KeyError(f"{key!r}: read_results returned no results: {self.reason}")


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
        logger.warning(
            "read_results: missing lib/cell (lib=%r cell=%r session=%r); "
            "session likely lacks an active test — pass lib= and cell= explicitly",
            lib, cell, session,
        )
        return NoResults(f"missing lib/cell for session {session!r}; pass lib= and cell=")

    test = _get_test(client, session)
    if not test:
        logger.warning(
            "read_results: maeGetSetup returned no test for session=%r "
            "(typical for fresh ADE Explorer sessions that haven't gone "
            "through maeSetupTest); returning empty",
            session,
        )
        return NoResults(f"maeGetSetup returned no test for session {session!r}")

    # Pick the latest history with actual results (newest-first scan).
    if history:
        latest_history = history.strip()
    else:
        latest_history = _find_latest_history_with_results(
            client, lib=lib, cell=cell, test=test, session=session)
    if not latest_history or latest_history == "nil":
        logger.warning(
            "read_results: no history with results for %s/%s test=%r "
            "(passed history=%r); returning empty",
            lib, cell, test, history,
        )
        return NoResults(f"no history with results for {lib}/{cell} test {test!r}")

    # Export the full Detail table to a remote tmp CSV; scp; parse.
    # maeExportOutputView's return contract is not portable across
    # Cadence versions -- some echo the filename, others just return
    # ``t``/``nil`` -- so the only reliable success signal is whether
    # the remote CSV materialised.  Let download_file be the arbiter.
    remote_csv = f"/tmp/vb_results_{uuid.uuid4().hex}.csv"
    export_cmd = _export_output_view_skill(
        remote_csv,
        # No ?testName: it narrows the table to one test, and the first
        # test of the setup may have no points in this history.
        f'?session "{session}" '
        f'?historyName "{latest_history}" ?view "Detail"',
    )
    export = client.execute_skill(export_cmd)
    skill_out = export.output or ""
    export_error = "; ".join(getattr(export, "errors", None) or [])

    local_csv = Path(tempfile.gettempdir()) / f"vb_results_{uuid.uuid4().hex}.csv"
    csv_text = ""
    try:
        if export_error:
            raise RuntimeError(export_error)
        client.download_file(remote_csv, str(local_csv))
        csv_text = local_csv.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning(
            "read_results: maeExportOutputView did not produce a fetchable "
            "CSV (remote=%s, skill_out=%r, exc=%s); returning empty.  "
            "Compute outputs from the PSF instead (export_waveform or OCEAN).",
            remote_csv, skill_out, exc,
        )
        return NoResults(f"maeExportOutputView did not produce a CSV for "
                         f"{latest_history}: {exc}")
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
                                       lib: str, cell: str, test: str,
                                       session: str = "") -> str:
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
    # Any test with outputs counts: the first test may not have run in a
    # history that only ran the others.
    tests_outputs = (
        f'foreach(mapcan vbT maeGetSetup(?typeName "tests" ?session "{session}") '
        f'copy(maeGetResultOutputs(?testName vbT)))'
        if session else f'maeGetResultOutputs(?testName "{test}")')
    # Without ?session the history opens in whichever session is current.
    session_arg = f' ?session "{session}"' if session else ""
    for h in reversed(hist_list):
        r = client.execute_skill(
            f'when(maeOpenResults(?history "{h}"{session_arg}) '
            f'  let((outs) '
            f'    outs = {tests_outputs} '
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
    corner_points = _parse_corner_columns(text)
    if corner_points is not None:
        return _detail_result(corner_points[0], corner_points[1], history)
    points: list[dict] = []
    current: dict | None = None
    tests_seen: set[str] = set()
    no_point_detail = False

    reader = csv.reader(text.splitlines())
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        first = (row[0] or "").strip()
        header = [c.strip() for c in row[:6]]
        if header == ["Test", "Output", "Nominal", "Spec", "Weight", "Pass/Fail"]:
            no_point_detail = True
            continue
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
        if first in ("", "Point", "Test"):
            if first == "Point":
                no_point_detail = False
            continue
        if not first.isdigit():
            if not no_point_detail:
                continue
            # Single-point runs in some Cadence versions omit the Point
            # column and export rows as:
            #   Test,Output,Nominal,Spec,Weight,Pass/Fail
            cols = row + [""] * (6 - len(row))
            test_n, name, value, spec, weight, pass_fail = cols[:6]
            if not name.strip():
                continue
            if current is None:
                current = {"point": 1, "parameters": {}, "outputs": {}}
                points.append(current)
            if test_n:
                tests_seen.add(test_n.strip())
            current["outputs"][name.strip()] = {
                "value":     value.strip(),
                "spec":      spec.strip(),
                "weight":    weight.strip(),
                "pass_fail": pass_fail.strip(),
            }
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

    return _detail_result(points, tests_seen, history)


def _detail_result(points: list[dict], tests_seen, history: str) -> dict:
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


# IC23.1 writes the Detail view with one column per point (corner/sweep)::
#
#     ,Parameter,,,,,,pn_tt_0p8,pn_ff_0p8
#     ,design_wrapper.lib.scs,,,,,,tt_pre,ff_pre
#     ,vdd,,,,,,800e-3,800e-3
#     <blank>
#     Test,Output,Spec,Weight,Pass/Fail,Min,Max,pn_tt_0p8,pn_ff_0p8
#     PNOISE,noise,< 1m,,pass,365e-6,528.7e-6,456e-6,528.7e-6
#
# Parameter rows share the point columns of the header row below them.
_DETAIL_FIXED_COLUMNS = {"Test", "Output", "Spec", "Weight", "Pass/Fail", "Min", "Max"}


def _parse_corner_columns(text: str):
    """Parse the per-point-column Detail layout; ``None`` for other layouts.  Pure."""
    rows = list(csv.reader(text.splitlines()))
    header_at = next((i for i, r in enumerate(rows)
                      if [c.strip() for c in r[:2]] == ["Test", "Output"]), None)
    if header_at is None:
        return None
    header = [c.strip() for c in rows[header_at]]
    if "Nominal" in header:
        return None
    columns = [(i, name) for i, name in enumerate(header)
               if name and name not in _DETAIL_FIXED_COLUMNS]
    if not columns:
        return None
    fixed = {name: i for i, name in enumerate(header) if name in _DETAIL_FIXED_COLUMNS}

    def cell(row, i):
        return row[i].strip() if i is not None and i < len(row) else ""

    points = [{"point": n + 1, "corner": name, "parameters": {}, "outputs": {}}
              for n, (_, name) in enumerate(columns)]
    for row in rows[:header_at]:
        name = cell(row, 1)
        if cell(row, 0) or not name or name == "Parameter":
            continue
        for point, (i, _) in zip(points, columns):
            value = cell(row, i)
            if value:
                point["parameters"][name] = value
    tests_seen: set[str] = set()
    for row in rows[header_at + 1:]:
        test_n, name = cell(row, fixed.get("Test")), cell(row, fixed.get("Output"))
        if not name:
            continue
        if test_n:
            tests_seen.add(test_n)
        for point, (i, _) in zip(points, columns):
            point["outputs"][name] = {
                "value":     cell(row, i),
                "spec":      cell(row, fixed.get("Spec")),
                "weight":    cell(row, fixed.get("Weight")),
                "pass_fail": cell(row, fixed.get("Pass/Fail")),
            }
    return points, tests_seen


def _unquote_atom(raw: str) -> str | None:
    s = (raw or "").strip().strip('"')
    if not s or s.lower() == "nil":
        return None
    return s


# ---------------------------------------------------------------------------
# check_points — per-point Spectre log status
# ---------------------------------------------------------------------------

# Lines of spectre.out that decide a point's status.
_SPECTRE_LOG_PATTERN = (
    r"ERROR \(|terminated prematurely|Segmentation fault|"
    r"internal error|completes with [0-9]+ error"
)
_SPECTRE_FAILED = re.compile(
    r"ERROR \(|terminated prematurely|Segmentation fault|internal error|"
    r"completes with [1-9][0-9]* error", re.IGNORECASE)
_SPECTRE_OK = re.compile(r"completes with 0 errors", re.IGNORECASE)


def _classify_spectre_log(lines) -> tuple[str, list[str]]:
    """Return ``("failed"|"ok"|"incomplete", error_lines)`` for a log.  Pure."""
    errors = [ln.strip() for ln in lines if _SPECTRE_FAILED.search(ln)]
    if errors:
        return "failed", errors[:10]
    if any(_SPECTRE_OK.search(ln) for ln in lines):
        return "ok", []
    return "incomplete", []


def check_points(client: VirtuosoClient, history: str, *,
                 lib: str, cell: str) -> list[dict]:
    """Status of every simulated point of *history*, from its Spectre logs.

    A finished Maestro run says nothing about failed points; a Spectre crash
    (SPECTRE-18/19, a segfault) still ends the run as "done". This reads each
    ``spectre.out`` under the history's results directory and returns::

        [{"log": "1/TRAN/psf/spectre.out", "point": "1",
          "status": "failed" | "ok" | "incomplete",
          "errors": ["ERROR (SPECTRE-18): ..."]}, ...]
    """
    history = (history or "").strip().strip('"')
    r = client.execute_skill(f'ddGetObj("{lib}")~>readPath')
    lib_path = (r.output or "").strip().strip('"')
    if not lib_path or lib_path == "nil":
        raise RuntimeError(f"check_points: library {lib!r} not found")
    tail = f"{lib}/{cell}/maestro/results/maestro/{history}"
    # The library keeps only the history's .rdb/.log; Maestro writes the
    # point directories under the ADE project directory (asimenv.startup
    # projectDir, e.g. /scratch/<user>/simulation), recorded in the .rdb.
    roots = [f"{lib_path}/{cell}/maestro/results/maestro/{history}"]
    logs: dict[str, list[str]] = {}
    searched: list[str] = []
    for root in _candidate_result_roots(client, roots, lib_path, cell, history, tail):
        if root in searched:
            continue
        searched.append(root)
        logs = _read_spectre_logs(client, root)
        if logs:
            break
    if not logs:
        raise RuntimeError(
            f"check_points: no spectre.out for history {history!r}; searched "
            + ", ".join(searched))

    points = []
    for log, lines in logs.items():
        status, errors = _classify_spectre_log(lines)
        first = log.split("/", 1)[0]
        points.append({"log": log, "point": first if first.isdigit() else "",
                       "status": status, "errors": errors})
    return points


def _candidate_result_roots(client, first, lib_path, cell, history, tail):
    """Yield possible run directories of *history*, cheapest first."""
    yield from first
    r = client.execute_skill('car(errset(envGetVal("asimenv.startup" "projectDir")))')
    project = (r.output or "").strip().strip('"')
    if project and project != "nil":
        yield f"{project.rstrip('/')}/{tail}"
    rdb = f"{lib_path}/{cell}/maestro/results/maestro/{history}.rdb"
    pattern = re.compile(r"(/[^\x00\s$]*/maestro/results/maestro/" + re.escape(history) + r")(?![\w.])")
    runner = client.ssh_runner
    if runner is None:
        try:
            data = Path(rdb).read_bytes().decode("latin-1")
        except OSError:
            data = ""
    else:
        cmd = (f"grep -a -o {shlex.quote('/[^[:space:]$]*/maestro/results/maestro/' + history)} "
               f"{shlex.quote(rdb)} | sort -u")
        out = runner.run_command(f"sh -c {shlex.quote(cmd)}")
        data = out.stdout if out.returncode == 0 else ""
    for match in sorted(set(pattern.findall(data))):
        yield match


def _read_spectre_logs(client, root: str) -> dict[str, list[str]]:
    logs: dict[str, list[str]] = {}
    runner = client.ssh_runner
    if runner is None:
        if not Path(root).is_dir():
            return logs
        for path in sorted(Path(root).rglob("spectre.out")):
            text = path.read_text(encoding="utf-8", errors="replace")
            logs[str(path.relative_to(root))] = [
                ln for ln in text.splitlines()
                if re.search(_SPECTRE_LOG_PATTERN, ln, re.IGNORECASE)]
        return logs
    q_root = shlex.quote(root)
    # sh -c: the login shell may be csh, which has no while/do loop.
    cmd = (f"test -d {q_root} || exit 0; cd {q_root} && find . -name spectre.out | sort | "
           f"while read f; do echo \"@@ $f\"; "
           f"grep -i -E {shlex.quote(_SPECTRE_LOG_PATTERN)} \"$f\" | head -20; done")
    out = runner.run_command(f"sh -c {shlex.quote(cmd)}")
    if out.returncode != 0:
        raise RuntimeError(f"check_points: cannot read {root}: {out.stderr.strip()}")
    current = None
    for line in out.stdout.splitlines():
        if line.startswith("@@ "):
            current = line[3:].removeprefix("./")
            logs[current] = []
        elif current is not None:
            logs[current].append(line)
    return logs


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
    precision: int | None = None,
    width: int | None = None,
    number_notation: str | None = None,
) -> str:
    """Export a waveform via OCEAN to a local text file.

    Args:
        session: session string (used to find history if not given)
        expression: OCEAN expression, e.g. ``'dB20(mag(VF("/VOUT")))'``
        local_path: where to save locally
        analysis: which analysis to select ("ac", "tran", "noise", etc.)
        history: explicit history name; auto-detected if empty
        precision: optional ocnPrint significant digits (1-16).  Omitted by
            default so the user's OCEAN setup remains authoritative.
        width: optional ocnPrint column width in characters (at least 4).
            To display every requested significant digit, use a width greater
            than or equal to precision.
        number_notation: optional ocnPrint number format ('suffix,
            'engineering, 'scientific, or 'none).  Defaults to 'scientific
            for backward compatibility.  'none skips per-value formatting
            and is significantly faster on large exports.

    Returns the local file path.
    """
    if precision is not None:
        if type(precision) is not int:
            raise TypeError("precision must be an int or None")
        if not 1 <= precision <= 16:
            raise ValueError("precision must be between 1 and 16")
    if width is not None:
        if type(width) is not int:
            raise TypeError("width must be an int or None")
        if width < 4:
            raise ValueError("width must be at least 4")
    valid_notations = {"suffix", "engineering", "scientific", "none"}
    if number_notation is not None:
        if type(number_notation) is not str:
            raise TypeError("number_notation must be a str or None")
        if number_notation not in valid_notations:
            raise ValueError(
                f"number_notation must be one of {sorted(valid_notations)}"
            )

    format_args = f" ?numberNotation '{number_notation or 'scientific'}"
    if precision is not None:
        format_args += f" ?precision {precision}"
    if width is not None:
        format_args += f" ?width {width}"

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
        f'ocnPrint({expression}{format_args} ?numSpaces 1 '
        f'?output "{remote_path}")')

    client.download_file(remote_path, local_path)
    client.execute_skill(f'deleteFile("{remote_path}")')
    return local_path

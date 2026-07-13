"""Post-processing fixes for Maestro CSV results.

Maestro's single-run ``maeExportOutputView ?view "Detail"`` CSV can have
7 columns instead of the documented 6, adding a "Nominal Spec" column that
shifts all subsequent fields.  The upstream ``virtuoso-bridge-lite`` parser
doesn't handle this layout, so we apply a post-processing fix here.

When the upstream package is updated to handle 7-col CSV natively,
the ``fix_maestro_results`` call in ``run.py`` can simply be removed.
"""

from __future__ import annotations

import csv
import re


def parse_single_run_csv(text: str, *, history: str = "") -> dict:
    """Parse a single-run Maestro Detail CSV with correct column mapping.

    Handles both 6-col and 7-col header layouts:

    - 6-col: Test, Output, Nominal, Spec, Weight, Pass/Fail
    - 7-col: Test, Output, Nominal Spec, Nominal, Spec, Weight, Pass/Fail

    The 7-col variant adds a "Nominal Spec" column (the spec expression)
    before the "Nominal" column (the measured value).

    Returns a dict with the same structure as bridge-lite's
    ``_parse_detail_csv``: ``{"history", "tests", "points", "outputs"}``.
    """
    points: list[dict] = []
    current: dict | None = None
    tests_seen: set[str] = set()

    rows = list(csv.reader(text.splitlines()))
    current = {"point": 1, "parameters": {}, "outputs": {}}
    points.append(current)
    in_data = False
    has_nominal_spec_col = False

    for row in rows:
        if not row or not any(c.strip() for c in row):
            continue
        first = (row[0] or "").strip()
        # Detect the header row
        if first == "Test" and len(row) > 1 and (row[1] or "").strip() == "Output":
            in_data = True
            col2 = (row[2] or "").strip() if len(row) > 2 else ""
            has_nominal_spec_col = "Nominal Spec" in col2
            continue
        if not in_data:
            continue

        if has_nominal_spec_col:
            # 7-col: Test, Output, Nominal Spec, Nominal, Spec, Weight, Pass/Fail
            cols = row + [""] * (7 - len(row))
            test_n = cols[0].strip()
            name = cols[1].strip()
            # cols[2] = Nominal Spec (spec expression, e.g. "> 0.9*1.8")
            value = cols[3].strip()     # actual measured value
            spec = cols[4].strip()      # spec limit expression
            weight = cols[5].strip()
            pass_fail = cols[6].strip()
        else:
            # 6-col: Test, Output, Nominal, Spec, Weight, Pass/Fail
            cols = row + [""] * (6 - len(row))
            test_n = cols[0].strip()
            name = cols[1].strip()
            value = cols[2].strip()
            spec = cols[3].strip()
            weight = cols[4].strip()
            pass_fail = cols[5].strip()

        if test_n:
            tests_seen.add(test_n)
        # Skip group-header rows (output name present but nominal value absent)
        if name and value:
            current["outputs"][name] = {
                "value": value,
                "spec": spec,
                "weight": weight,
                "pass_fail": pass_fail,
            }

    # Back-compat flat list
    flat_outputs: list[dict] = []
    for p in points:
        for name, info in p["outputs"].items():
            flat_outputs.append({
                "point": p["point"],
                "name": name,
                "value": info["value"],
                "spec_status": info["pass_fail"],
            })

    return {
        "history": history,
        "tests": sorted(tests_seen),
        "points": points,
        "outputs": flat_outputs,
    }


def fix_maestro_results(results: dict) -> dict:
    """Post-process Maestro results to fix 7-col CSV mis-parsing.

    Detects the 7-column CSV format issue by checking if any output
    ``value`` field looks like a spec expression (starts with ``>`` or ``<``).
    If so, re-parses the ``raw_csv`` field with the corrected parser.

    This is a no-op if:
    - No ``raw_csv`` is available
    - All values parse correctly (6-col format, or upstream already fixed)

    Parameters
    ----------
    results : dict
        Output from ``virtuoso_bridge.virtuoso.maestro.read_results()``.

    Returns
    -------
    dict
        The same dict with ``points`` and ``outputs`` replaced if a fix
        was applied.
    """
    raw_csv = results.get("raw_csv")
    if not raw_csv:
        return results

    # If upstream returned empty points, always try re-parsing with our
    # single-run CSV parser — the upstream reader may fail entirely on the
    # 7-col format (extra "Nominal Spec" column).
    has_points = bool(results.get("points"))

    if has_points:
        # Check if any output value looks like a spec expression (mis-parsed)
        needs_fix = False
        for pt in results.get("points", []):
            for out_name, info in (pt.get("outputs") or {}).items():
                val = (info.get("value", "") or "").strip()
                if val and (val.startswith(">") or val.startswith("<")):
                    needs_fix = True
                    break
            if needs_fix:
                break

        if not needs_fix:
            return results

    # Re-parse with the fixed single-run CSV parser
    history = results.get("history", "")
    fixed = parse_single_run_csv(raw_csv, history=history)

    # Replace the mis-parsed fields, keep everything else
    results["points"] = fixed["points"]
    results["outputs"] = fixed["outputs"]
    results["tests"] = fixed["tests"]

    print(f"[maestro-reader] Applied 7-col CSV fix: "
          f"{sum(len(p.get('outputs', {})) for p in fixed['points'])} outputs recovered")

    return results

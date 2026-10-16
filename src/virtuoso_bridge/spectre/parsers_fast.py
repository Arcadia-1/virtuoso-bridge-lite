"""Fast PSF ASCII parser (drop-in, stdlib only).

Why it is fast:
- One pass over file (old code reads text twice).
- Pre-compiled regex, no per-line re.match compile.
- Streams lines, no big splitlines list.
- Step-interpolate only on demand.
- Parallel directory parse with threads.
"""
from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_RE_COMPLEX = re.compile(r'"([^"]+)"\s+\(\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\)')
_RE_KV = re.compile(r'"([^"]+)"\s+(\S+)')
_RE_QUOTED = re.compile(r'"([^"]+)"')
_RE_GROUP = re.compile(r'"(\s*\d+)"\s+GROUP\s+\d+')
_RE_SIG_UNIT = re.compile(r'"([^"]+)"\s+"[^"]*"')

_SECTIONS = {"HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "END"}


def parse_file_fast(psf_path: Path, want: list[str] | None = None) -> Dict[str, Any]:
    """Parse one PSF ASCII file. Returns {signal: values}.

    ``want`` limits materialized signals (plus sweep var). Scan cost
    stays linear, but memory and post work drop with signal count.
    Use it when the caller needs 3 of 300 signals.
    """
    try:
        fp = open(psf_path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with fp:
        return _parse_stream(fp, set(want) if want else None)


def _parse_stream(fp, want: set[str] | None = None) -> Dict[str, Any]:
    sweep_var = ""
    trace_names: List[str] = []
    group_to_name: Dict[str, str] = {}
    cur_group = None
    section = ""
    # Pass 1: collect header vars without storing all lines
    raw: List[Tuple[Any, Any]] = []
    in_value = False
    for line in fp:
        s = line.strip()
        if s in _SECTIONS:
            section = s
            in_value = section == "VALUE"
            if s == "END":
                break
            continue
        if section == "SWEEP" and not sweep_var:
            m = _RE_QUOTED.match(s)
            if m:
                sweep_var = m.group(1)
        elif section == "TRACE":
            mg = _RE_GROUP.match(s)
            if mg:
                cur_group = mg.group(1)
                continue
            ms = _RE_SIG_UNIT.match(s)
            if ms:
                name = ms.group(1)
                trace_names.append(name)
                if cur_group is not None:
                    group_to_name[cur_group] = name
                cur_group = None
                continue
            m = _RE_QUOTED.match(s)
            if m:
                trace_names.append(m.group(1))
        elif in_value:
            if s == "END" or not s:
                if s == "END":
                    break
                continue
            mc = _RE_COMPLEX.match(s)
            if mc:
                key = group_to_name.get(mc.group(1), mc.group(1))
                try:
                    raw.append((key, complex(float(mc.group(2)), float(mc.group(3)))))
                except ValueError:
                    pass
                continue
            m = _RE_KV.match(s)
            if m:
                key, val = m.group(1), m.group(2)
                if key == sweep_var and sweep_var:
                    try:
                        raw.append((None, float(val)))
                    except ValueError:
                        pass
                else:
                    key = group_to_name.get(key, key)
                    try:
                        raw.append((key, float(val)))
                    except ValueError:
                        pass
    if not sweep_var:
        return {}
    # Pass 2: step-interpolate (same math as old code, less alloc)
    nan = math.nan
    time_vals: List[Any] = []
    state: Dict[str, Any] = {}
    keep = set(trace_names) if want is None else (set(trace_names) & want)
    series: Dict[str, List[Any]] = {n: [] for n in keep}
    append_all = lambda: [series[n].append(state.get(n, nan)) for n in keep]
    for key, val in raw:
        if key is None:
            if time_vals:
                append_all()
            time_vals.append(val)
        elif want is None or key in want:
            state[key] = val
    if time_vals:
        append_all()
    data: Dict[str, Any] = {sweep_var: time_vals}
    data.update(series)
    return data


def signal_stats(values: list) -> dict:
    """One-pass n/min/max/mean/rms/first/last. Non-numeric skipped."""
    n = 0
    total = 0.0
    total_sq = 0.0
    vmin = None
    vmax = None
    first = None
    last = None
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if v != v:  # NaN guard: missing delta-fill is not data
            continue
        n += 1
        total += v
        total_sq += v * v
        vmin = v if vmin is None or v < vmin else vmin
        vmax = v if vmax is None or v > vmax else vmax
        if first is None:
            first = v
        last = v
    if not n:
        return {"n": len(values), "numeric": 0}
    mean = total / n
    var = max(total_sq / n - mean * mean, 0.0)
    return {"n": len(values), "numeric": n, "min": vmin, "max": vmax,
            "mean": mean, "rms": math.sqrt(var), "first": first, "last": last}


def parse_dir_fast(output_dir: Path, max_workers: int = 4) -> Dict[str, Any]:
    """Parse one raw dir in parallel. Returns merged {signal: values}."""
    files: List[Tuple[str, Path]] = []
    for pat in ("*.tran.tran", "*.tran", "*.ac", "*.ac.ac", "*.dc"):
        hits = sorted(output_dir.glob(pat))
        if hits:
            files.append((pat, hits[0]))
            if pat.startswith("*.tran"):
                break
    if not files:
        return {}
    merged: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(files))) as ex:
        results = list(ex.map(lambda t: parse_file_fast(t[1]), files))
    for (pat, _), data in zip(files, results):
        if not data:
            continue
        prefix = ""
        if ".ac" in pat:
            prefix = "ac_"
        elif pat.endswith(".dc"):
            prefix = "dc_"
        for k, v in data.items():
            merged[prefix + k] = v
    return merged


def measure_period(t: list, v: list, level: float | None = None) -> dict:
    """Rising-edge period/freq from one waveform. {} when fewer than 2 edges."""
    import statistics
    pts = [(ti, vi) for ti, vi in zip(t, v)
           if isinstance(ti, (int, float)) and isinstance(vi, (int, float))
           and ti == ti and vi == vi]
    if len(pts) < 4:
        return {}
    vals = [vi for _, vi in pts]
    lo, hi = min(vals), max(vals)
    if hi - lo <= 0:
        return {}
    lv = level if level is not None else lo + 0.5 * (hi - lo)
    cross = [pts[i][0] for i in range(1, len(pts)) if pts[i - 1][1] < lv <= pts[i][1]]
    if len(cross) < 2:
        return {}
    pers = [(b - a) for a, b in zip(cross, cross[1:])]
    per = statistics.median(pers)
    return {"edges": len(cross), "period_s": per, "freq_hz": 1.0 / per,
            "swing": [lo, hi]}

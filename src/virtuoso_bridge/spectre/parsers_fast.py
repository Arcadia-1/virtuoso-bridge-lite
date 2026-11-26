"""Optional swept PSF ASCII parser (stdlib only).

This experimental reader is not a drop-in replacement for every PSF format.
It reads file lines once, retains value events, then materializes selected
signals. Directory parsing uses threads; no performance benchmark is claimed.
"""
from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_PSF_FLOAT = r"(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:nan|inf(?:inity)?))"
_RE_COMPLEX = re.compile(
    rf'"([^"]+)"\s+\(\s*({_PSF_FLOAT})\s+({_PSF_FLOAT})\s*\)',
    re.IGNORECASE,
)
_RE_TYPED_COMPLEX = re.compile(
    rf'"([^"]+)"\s+"[^"]*"\s+\(\s*({_PSF_FLOAT})\s+({_PSF_FLOAT})\s*\)',
    re.IGNORECASE,
)
_RE_TYPED_NUM = re.compile(
    rf'"([^"]+)"\s+"[^"]*"\s+({_PSF_FLOAT})(?=\s|$)',
    re.IGNORECASE,
)
_RE_TYPED_STR = re.compile(r'"([^"]+)"\s+"[^"]*"\s+"((?:[^"\\]|\\.)*)"')
_RE_UNTYPED_STR = re.compile(r'"([^"]+)"\s+"((?:[^"\\]|\\.)*)"')
_RE_KV = re.compile(r'"([^"]+)"\s+(\S+)')
_RE_QUOTED = re.compile(r'"([^"]+)"')
_RE_GROUP = re.compile(r'"([^"]+)"\s+GROUP\s+\d+')
_RE_SIG_UNIT = re.compile(r'"([^"]+)"\s+"[^"]*"')

_SECTIONS = {"HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "END"}


def _unescape_psf_string(raw: str) -> str:
    return raw.replace('\\"', '"').replace("\\\\", "\\")


def _strip_prop_from_line(line: str, prop_depth: int) -> Tuple[str, int]:
    """Strip balanced ``PROP(...)`` blocks across lines, ignoring quoted text."""
    out: List[str] = []
    in_quotes = False
    escaped = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if prop_depth > 0:
            if escaped:
                escaped = False
            elif in_quotes:
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_quotes = False
            elif ch == '"':
                in_quotes = True
            elif ch == "(":
                prop_depth += 1
            elif ch == ")":
                prop_depth -= 1
            i += 1
            continue

        if in_quotes:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quotes = False
            i += 1
            continue

        if (
            ch == "P"
            and line[i : i + 4] == "PROP"
            and (i == 0 or not (line[i - 1].isalnum() or line[i - 1] == "_"))
        ):
            j = i + 4
            while j < n and line[j].isspace():
                j += 1
            if j < n and line[j] == "(":
                prop_depth = 1
                i = j + 1
                continue

        if ch == '"':
            in_quotes = True
        out.append(ch)
        i += 1

    return "".join(out).strip(), prop_depth


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
    saw_sweep = False
    trace_names: List[str] = []
    seen_traces: set[str] = set()
    group_to_name: Dict[str, str] = {}
    cur_group = None
    section = ""
    prop_depth = 0
    # Pass 1: collect header vars without storing all lines
    raw: List[Tuple[Any, Any]] = []
    scalars: Dict[str, Any] = {}
    in_value = False
    for line in fp:
        s = line.strip()
        if s in _SECTIONS:
            section = s
            in_value = section == "VALUE"
            prop_depth = 0
            if s == "SWEEP":
                saw_sweep = True
            elif s == "END":
                break
            continue
        if not s:
            continue
        if section in ("SWEEP", "TRACE", "VALUE") and (prop_depth or "PROP" in s):
            s, prop_depth = _strip_prop_from_line(s, prop_depth)
            if not s:
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
                if name not in seen_traces:
                    trace_names.append(name)
                    seen_traces.add(name)
                if cur_group is not None:
                    group_to_name[cur_group] = name
                cur_group = None
                continue
            m = _RE_QUOTED.match(s)
            if m:
                name = m.group(1)
                if name not in seen_traces:
                    trace_names.append(name)
                    seen_traces.add(name)
                if cur_group is not None:
                    group_to_name[cur_group] = name
                    cur_group = None
        elif in_value:
            if saw_sweep:
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
            else:
                mtc = _RE_TYPED_COMPLEX.match(s)
                if mtc:
                    key = mtc.group(1)
                    if want is None or key in want:
                        try:
                            scalars[key] = complex(float(mtc.group(2)), float(mtc.group(3)))
                        except ValueError:
                            pass
                    continue
                mc = _RE_COMPLEX.match(s)
                if mc:
                    key = mc.group(1)
                    if want is None or key in want:
                        try:
                            scalars[key] = complex(float(mc.group(2)), float(mc.group(3)))
                        except ValueError:
                            pass
                    continue
                mtn = _RE_TYPED_NUM.match(s)
                if mtn:
                    key = mtn.group(1)
                    if want is None or key in want:
                        try:
                            scalars[key] = float(mtn.group(2))
                        except ValueError:
                            pass
                    continue
                mts = _RE_TYPED_STR.match(s)
                if mts:
                    key = mts.group(1)
                    if want is None or key in want:
                        scalars[key] = _unescape_psf_string(mts.group(2))
                    continue
                m = _RE_KV.match(s)
                if m:
                    key, val = m.group(1), m.group(2)
                    if want is None or key in want:
                        try:
                            scalars[key] = float(val)
                        except ValueError:
                            mus = _RE_UNTYPED_STR.match(s)
                            if mus:
                                scalars[key] = _unescape_psf_string(mus.group(2))
    if not saw_sweep:
        return scalars
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
        if not math.isfinite(v):  # NaN / +/-inf guard: non-finite is not data
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
    return {"n": len(values), "numeric": n, "min": vmin, "max": vmax,
            "mean": mean, "rms": math.sqrt(total_sq / n), "first": first, "last": last}


def parse_dir_fast(output_dir: Path, max_workers: int = 4) -> Dict[str, Any]:
    """Merge analysis files: transient, AC (ac_), and all DC (*.dc) results."""
    files: List[Tuple[str, Path]] = []
    for prefix, patterns in (("", ("*.tran.tran", "*.tran")),
                             ("ac_", ("*.ac",))):
        for pattern in patterns:
            hits = sorted(output_dir.glob(pattern))
            if hits:
                files.append((prefix, hits[0]))
                # Only stop this analysis's fallback search, not AC/DC.
                break
    dc_hits = sorted(output_dir.glob("*.dc"))
    if len(dc_hits) == 1:
        files.append(("dc_", dc_hits[0]))
    elif dc_hits:
        by_name = {p.name: p for p in dc_hits}
        primary = next(
            (by_name[name] for name in ("dc.dc", "dcOp.dc", "spectre.dc") if name in by_name),
            None,
        )
        for dc_path in dc_hits:
            prefix = (
                "dc_"
                if dc_path == primary
                else f"{dc_path.stem.replace('.', '_')}_"
            )
            files.append((prefix, dc_path))
    if not files:
        return {}
    merged: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(files))) as ex:
        results = list(ex.map(lambda t: parse_file_fast(t[1]), files))
    for (prefix, _), data in zip(files, results):
        if not data:
            continue
        for k, v in data.items():
            merged[prefix + k] = v
    return merged


def measure_period(t: list, v: list, level: float | None = None) -> dict:
    """Rising-edge period/freq from one waveform. {} when fewer than 2 edges."""
    import statistics
    if level is not None and (
        isinstance(level, bool)
        or not isinstance(level, (int, float))
        or not math.isfinite(level)
    ):
        return {}
    pts = [(ti, vi) for ti, vi in zip(t, v)
           if not isinstance(ti, bool) and not isinstance(vi, bool)
           and isinstance(ti, (int, float)) and isinstance(vi, (int, float))
           and math.isfinite(ti) and math.isfinite(vi)]
    if len(pts) < 4:
        return {}
    vals = [vi for _, vi in pts]
    lo, hi = min(vals), max(vals)
    if hi - lo <= 0:
        return {}
    lv = level if level is not None else lo + 0.5 * (hi - lo)
    cross: List[float] = []
    for i in range(1, len(pts)):
        if pts[i - 1][1] < lv <= pts[i][1]:
            t_cross = pts[i][0]
            if not cross or t_cross > cross[-1]:
                cross.append(t_cross)
    if len(cross) < 2:
        return {}
    pers = [b - a for a, b in zip(cross, cross[1:]) if b - a > 0]
    if not pers:
        return {}
    per = statistics.median(pers)
    if per <= 0:
        return {}
    return {"edges": len(cross), "period_s": per, "freq_hz": 1.0 / per,
            "swing": [lo, hi]}

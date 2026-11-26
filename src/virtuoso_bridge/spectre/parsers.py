"""Spectre PSF ASCII simulation result parsing."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from virtuoso_bridge.models import ExecutionStatus, SimulationResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_spectre_psf_ascii(psf_path: Path) -> SimulationResult:
    """Parse a single Spectre PSF ASCII file into a SimulationResult."""
    if not psf_path.exists():
        return SimulationResult(
            status=ExecutionStatus.ERROR,
            errors=[f"PSF ASCII file not found: {psf_path}"],
        )

    try:
        content = psf_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return SimulationResult(
            status=ExecutionStatus.ERROR,
            errors=[f"Failed to read PSF ASCII file: {exc}"],
        )

    if not content.strip():
        return SimulationResult(
            status=ExecutionStatus.ERROR,
            errors=["PSF ASCII file is empty"],
        )

    data = _parse_psf_ascii_content(content)
    header = _parse_psf_ascii_header(content)

    metadata: dict[str, Any] = {}
    if header:
        metadata["psf_header"] = header

    status = ExecutionStatus.SUCCESS if data else ExecutionStatus.FAILURE
    return SimulationResult(status=status, data=data, metadata=metadata)

def _spectre_psf_scan_root(raw_dir: Path) -> Path:
    """Resolve directory that holds PSF ASCII files."""
    if not raw_dir.exists() or not raw_dir.is_dir():
        return raw_dir
    inner = raw_dir / raw_dir.name
    if inner.is_dir():
        has_psf = (
            any(inner.glob("*.dc"))
            or any(inner.glob("*.info"))
            or any(inner.glob("*.tran"))
            or any(inner.glob("*.ac"))
            or inner.joinpath("logFile").is_file()
        )
        if has_psf:
            return inner
    for child in sorted(raw_dir.iterdir()):
        if not child.is_dir():
            continue
        if (
            any(child.glob("*.dc"))
            or any(child.glob("*.info"))
            or any(child.glob("*.tran"))
            or any(child.glob("*.ac"))
        ):
            return child
    return raw_dir

def parse_psf_ascii_directory(output_dir: Path) -> dict[str, Any]:
    """Parse all PSF ASCII files in a Spectre output directory."""
    merged_data: dict[str, Any] = {}

    if not output_dir.exists():
        return merged_data

    output_dir = _spectre_psf_scan_root(output_dir)

    tran_candidates = (
        "tran.tran.tran",
        "tran.tran",
    )
    tran_found = False
    for candidate in tran_candidates:
        tran_file = output_dir / candidate
        if tran_file.exists():
            result = parse_spectre_psf_ascii(tran_file)
            if result.data:
                merged_data.update(result.data)
                logger.debug(
                    "Parsed transient data from %s: %d signals",
                    tran_file.name,
                    len(result.data),
                )
            tran_found = True
            break
    if not tran_found:
        tran_globs = list(sorted(output_dir.glob("*.tran.tran")))
        seen_tran = set(tran_globs)
        tran_globs.extend(p for p in sorted(output_dir.glob("*.tran")) if p not in seen_tran)
        for tran_file in tran_globs:
            result = parse_spectre_psf_ascii(tran_file)
            if result.data:
                merged_data.update(result.data)
                logger.debug(
                    "Parsed transient data from %s: %d signals",
                    tran_file.name,
                    len(result.data),
                )
                break

    dc_candidates = ["dc.dc", "dcOp.dc", "spectre.dc"]
    dc_parsed = False
    for candidate in dc_candidates:
        dc_file = output_dir / candidate
        if not dc_file.exists():
            continue
        result = parse_spectre_psf_ascii(dc_file)
        if result.data:
            for key, val in result.data.items():
                merged_data[f"dc_{key}"] = val
            logger.debug(
                "Parsed DC data from %s: %d signals",
                dc_file.name,
                len(result.data),
            )
            dc_parsed = True
            break
    if not dc_parsed:
        for dc_file in sorted(output_dir.glob("*.dc")):
            if dc_file.name in ("dc.dc", "dcOp.dc"):
                continue
            result = parse_spectre_psf_ascii(dc_file)
            if not result.data:
                continue
            stem = dc_file.stem.replace(".", "_")
            for key, val in result.data.items():
                merged_data[f"{stem}_{key}"] = val
            logger.debug(
                "Parsed DC data from %s: %d signals",
                dc_file.name,
                len(result.data),
            )
            dc_parsed = True
            break
    if not dc_parsed:
        for name in ("dcOp.dc", "dc.dc", "spectre.dc"):
            hits = sorted(output_dir.rglob(name))
            if not hits:
                continue
            result = parse_spectre_psf_ascii(hits[0])
            if not result.data:
                continue
            for key, val in result.data.items():
                merged_data[f"dc_{key}"] = val
            logger.debug(
                "Parsed DC data from nested %s: %d signals",
                hits[0],
                len(result.data),
            )
            dc_parsed = True
            break

    ac_candidates = ("ac.ac", "ac.ac.ac")
    ac_found = False
    for candidate in ac_candidates:
        ac_file = output_dir / candidate
        if ac_file.exists():
            result = parse_spectre_psf_ascii(ac_file)
            if result.data:
                for key, val in result.data.items():
                    merged_data[f"ac_{key}"] = val
                logger.debug(
                    "Parsed AC data from %s: %d signals",
                    ac_file.name,
                    len(result.data),
                )
            ac_found = True
            break
    if not ac_found:
        ac_globs = list(sorted(output_dir.glob("*.ac.ac")))
        seen_ac = set(ac_globs)
        ac_globs.extend(p for p in sorted(output_dir.glob("*.ac")) if p not in seen_ac)
        for ac_file in ac_globs:
            result = parse_spectre_psf_ascii(ac_file)
            if result.data:
                for key, val in result.data.items():
                    merged_data[f"ac_{key}"] = val
                logger.debug(
                    "Parsed AC data from %s: %d signals",
                    ac_file.name,
                    len(result.data),
                )
                break

    for info_file in sorted(output_dir.rglob("*.info")):
        result = parse_spectre_psf_ascii(info_file)
        if result.data:
            prefix = info_file.stem.replace(".", "_")
            for key, val in result.data.items():
                merged_data[f"{prefix}_{key}"] = val

    return merged_data


def parse_sweep_psf_directory(output_dir: Path) -> dict[int, dict[str, Any]]:
    """Parse Spectre parametric-sweep output directory.

    Two naming conventions are supported:

    1. Subdirectory-per-point (classic Spectre):
       ``<raw>/sw1.sweep1/1/tran.tran.tran``

    2. Flat per-point files (Spectre X/LX +preset=lx):
       ``<raw>/sw1-000_tran.tran.tran``
       ``<raw>/sw1-001_tran.tran.tran``
       ...

    Returns ``{point_index: {signal: values}}`` where point_index starts
    at 1.  Returns empty dict if no sweep layout is recognised — caller
    should then fall back to :func:`parse_psf_ascii_directory` for
    single-point output.

    Note: this function is wired into :class:`SpectreSimulator` already —
    sweep results surface via ``result.metadata["sweep_points"]``.
    Direct callers only need this entry point when reading raw output
    dirs outside the simulator class.

    Example::

        >>> from virtuoso_bridge.spectre.parsers import parse_sweep_psf_directory
        >>> sweep = parse_sweep_psf_directory(Path("/tmp/sim/raw"))
        >>> if sweep:
        ...     for pt_idx, signals in sorted(sweep.items()):
        ...         vout_final = signals["v_out"][-1]
        ...         print(f"point {pt_idx}: v_out(t_end) = {vout_final}")
    """
    scan_root = _spectre_psf_scan_root(output_dir)
    sweep_data: dict[int, dict[str, Any]] = {}

    # Look for sweep subdirectories: sw<N>.sweep<N> containing numbered dirs
    for sweep_dir in sorted(scan_root.glob("sw*.sweep*")):
        if not sweep_dir.is_dir():
            continue
        for point_dir in sorted(sweep_dir.iterdir()):
            if not point_dir.is_dir():
                continue
            try:
                point_idx = int(point_dir.name)
            except ValueError:
                continue
            point_data = parse_psf_ascii_directory(point_dir)
            if point_data:
                sweep_data.setdefault(point_idx, {}).update(point_data)

    if sweep_data:
        return sweep_data

    # Fallback: flat per-point files (Spectre X/LX mode)
    # Files named sw1-000_tran.tran.tran, sw1-001_tran.tran.tran, ...
    # Spectre X/LX uses zero-padded indices (000, 001, ...); extract the
    # numeric part and convert from 0-indexed to 1-indexed to match the
    # subdirectory convention (1/, 2/, ...).
    for psf_file in sorted(scan_root.glob("sw*-[0-9]*_*")):
        if not psf_file.is_file():
            continue
        m = re.match(r"sw\d+-(\d+)_.+", psf_file.name)
        if not m:
            continue
        point_idx = int(m.group(1)) + 1
        result = parse_spectre_psf_ascii(psf_file)
        if result.data:
            sweep_data.setdefault(point_idx, {}).update(result.data)

    return sweep_data

# ---------------------------------------------------------------------------
# Parsing internals
# ---------------------------------------------------------------------------

def _parse_psf_ascii_header(content: str) -> dict[str, str]:
    """Extract key-value pairs from the HEADER section."""
    header: dict[str, str] = {}
    in_header = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "HEADER":
            in_header = True
            continue
        if stripped in ("TYPE", "SWEEP", "TRACE", "VALUE", "END"):
            break
        if not in_header:
            continue

        m = re.match(r'"([^"]+)"\s+"([^"]*)"', stripped)
        if m:
            header[m.group(1)] = m.group(2)
            continue
        m = re.match(r'"([^"]+)"\s+(\S+)', stripped)
        if m:
            header[m.group(1)] = m.group(2)

    return header

def _parse_psf_ascii_content(content: str) -> dict[str, Any]:
    """Dispatch to swept or non-swept parser based on section markers."""
    lines = content.splitlines()
    n = len(lines)

    sections: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in ("HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "END"):
            sections[stripped] = i

    if "VALUE" not in sections:
        return {}

    if "SWEEP" in sections:
        return _parse_psf_swept_data(lines, n, sections)
    return _parse_psf_non_swept_data(lines, n, sections)

def _parse_psf_swept_data(
    lines: list[str],
    n: int,
    sections: dict[str, int],
) -> dict[str, Any]:
    """Parse swept PSF ASCII data (transient / DC sweep / AC).

    Handles Spectre's delta-compressed output: the first time step
    contains all signal values; subsequent steps only include signals
    that changed.  Step-interpolation fills in missing values so every
    signal has the same length as the sweep variable.
    """
    # Sweep variable name
    sweep_var = ""
    sweep_start = sections["SWEEP"] + 1
    sweep_end = sections.get("TRACE", sections.get("VALUE", n))
    prop_depth = 0
    for i in range(sweep_start, sweep_end):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if stripped in ("TRACE", "VALUE", "END"):
            break
        if prop_depth or "PROP" in stripped:
            stripped, prop_depth = _strip_prop_from_line(stripped, prop_depth)
            if not stripped:
                continue
        m = re.match(r'"([^"]+)"', stripped)
        if m:
            sweep_var = m.group(1)
            break

    # Trace (dependent variable) names and optional GROUP→signal mapping.
    # PSF ASCII TRACE section uses pairs:
    #   " N" GROUP 1
    #   "signal_name" "V"
    # The VALUE section references by N, not by signal name.
    # We build a mapping from N → signal_name so data is stored under
    # the human-readable name.
    trace_names: list[str] = []
    seen_traces: set[str] = set()
    group_to_name: dict[str, str] = {}  # " 401" → "CLK_NET"
    current_group: str | None = None
    if "TRACE" in sections:
        trace_start = sections["TRACE"] + 1
        trace_end = sections.get("VALUE", n)
        prop_depth = 0
        for i in range(trace_start, trace_end):
            stripped = lines[i].strip()
            if not stripped:
                continue
            if stripped in ("VALUE", "END"):
                break
            if prop_depth or "PROP" in stripped:
                stripped, prop_depth = _strip_prop_from_line(stripped, prop_depth)
                if not stripped:
                    continue
            # GROUP marker: " N" GROUP 1 (or named "group" GROUP 1)
            m_group = re.match(r'"([^"]+)"\s+GROUP\s+\d+', stripped)
            if m_group:
                current_group = m_group.group(1)
                continue
            # Signal definition: "name" "V" or "name" "I" etc.
            m_sig = re.match(r'"([^"]+)"\s+"[^"]*"', stripped)
            if m_sig:
                sig_name = m_sig.group(1)
                if sig_name not in seen_traces:
                    trace_names.append(sig_name)
                    seen_traces.add(sig_name)
                if current_group is not None:
                    group_to_name[current_group] = sig_name
                current_group = None
                continue
            # Fallback: bare quoted name (no unit)
            m = re.match(r'"([^"]+)"', stripped)
            if m:
                sig_name = m.group(1)
                if sig_name not in seen_traces:
                    trace_names.append(sig_name)
                    seen_traces.add(sig_name)
                if current_group is not None:
                    group_to_name[current_group] = sig_name
                    current_group = None

    if not sweep_var:
        return {}

    # --- Two-pass approach for delta-compressed PSF ---
    # Pass 1: Collect raw (key, value) pairs and time markers.
    # Pass 2: Step-interpolate to build equal-length signal vectors.
    value_start = sections["VALUE"] + 1
    value_end = sections.get("END", n)

    # Parse all VALUE entries into a flat list of (key, value) pairs,
    # with None marking time-step boundaries.
    raw_entries: list[tuple[str | None, float | complex | None]] = []
    prop_depth = 0
    for i in range(value_start, value_end):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if stripped == "END":
            break
        if prop_depth or "PROP" in stripped:
            stripped, prop_depth = _strip_prop_from_line(stripped, prop_depth)
            if not stripped:
                continue
        # Complex value: "name" (real imag) → store as Python complex
        m_complex = re.match(
            rf'"([^"]+)"\s+\(\s*({_PSF_FLOAT})\s+({_PSF_FLOAT})\s*\)',
            stripped,
            re.IGNORECASE,
        )
        if m_complex:
            raw_key = m_complex.group(1)
            sig_name = group_to_name.get(raw_key, raw_key)
            try:
                real = float(m_complex.group(2))
                imag = float(m_complex.group(3))
                value = complex(real, imag)
            except ValueError:
                continue
            raw_entries.append((sig_name, value))
            continue
        # Scalar value: "name" value
        m = re.match(r'"([^"]+)"\s+(\S+)', stripped)
        if m:
            raw_key = m.group(1)
            if raw_key == sweep_var:
                # Time-step boundary marker
                try:
                    t_val: float | complex = float(m.group(2))
                except ValueError:
                    continue
                raw_entries.append((None, t_val))
            else:
                sig_name = group_to_name.get(raw_key, raw_key)
                try:
                    value = float(m.group(2))
                except ValueError:
                    continue
                raw_entries.append((sig_name, value))

    # Pass 2: Step-interpolate — build time vector and per-signal vectors.
    # Each time-step boundary resets which signals to record at that step.
    # Signals not yet seen at a snapshot point use NaN, not 0.0 — a real
    # 0V reading and a "this signal hasn't appeared yet in the stream"
    # state are otherwise indistinguishable to consumers.
    import math as _math
    _MISSING = _math.nan

    time_values: list[float | complex] = []
    # Current value of each signal (carry-forward / step-interpolation)
    signal_state: dict[str, float | complex] = {}
    # Output: per-signal list of values at each time step
    signal_series: dict[str, list[float | complex]] = {
        name: [] for name in trace_names
    }

    for key, value in raw_entries:
        if key is None:
            # Time-step boundary: snapshot all signals at the previous step
            if time_values:
                for name in signal_series:
                    signal_series[name].append(signal_state.get(name, _MISSING))
            time_values.append(value)  # type: ignore[arg-type]
        else:
            signal_state[key] = value  # type: ignore[assignment]

    # Snapshot at the last time step
    if time_values:
        for name in signal_series:
            signal_series[name].append(signal_state.get(name, _MISSING))

    data: dict[str, list[float | complex]] = {sweep_var: time_values}
    data.update(signal_series)

    # Sanity-check lengths
    expected = len(time_values)
    for name in trace_names:
        actual = len(data.get(name, []))
        if actual != expected:
            logger.warning(
                "PSF ASCII: signal '%s' has %d points, expected %d",
                name, actual, expected,
            )

    return data  # type: ignore[return-value]

_PSF_FLOAT = r"(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:nan|inf(?:inity)?))"
_PSF_QUOTED = r'"((?:[^"\\]|\\.)*)"'
_RE_PSF_TYPED_PAREN = re.compile(rf"^{_PSF_QUOTED}\s+{_PSF_QUOTED}\s+\((.*)$")
_RE_PSF_TYPED_STR = re.compile(rf"^{_PSF_QUOTED}\s+{_PSF_QUOTED}\s+{_PSF_QUOTED}(.*)$")
_RE_PSF_TYPED_NUM = re.compile(
    rf"^{_PSF_QUOTED}\s+{_PSF_QUOTED}\s+({_PSF_FLOAT})(?=\s|$)(.*)$",
    re.IGNORECASE,
)
_RE_PSF_TYPED_TOKEN = re.compile(rf"^{_PSF_QUOTED}\s+{_PSF_QUOTED}\s+(\S+)(.*)$")
_RE_PSF_UNTYPED_COMPLEX = re.compile(
    rf"^{_PSF_QUOTED}\s+\(\s*({_PSF_FLOAT})\s+({_PSF_FLOAT})\s*\)(.*)$",
    re.IGNORECASE,
)
_RE_PSF_UNTYPED_NUM = re.compile(
    rf"^{_PSF_QUOTED}\s+({_PSF_FLOAT})(?=\s|$)(.*)$",
    re.IGNORECASE,
)
_RE_PSF_UNTYPED_STR = re.compile(rf"^{_PSF_QUOTED}\s+{_PSF_QUOTED}(.*)$")
_RE_PSF_UNTYPED_TOKEN = re.compile(rf"^{_PSF_QUOTED}\s+(\S+)(.*)$")
_RE_PSF_COMPLEX_PAIR = re.compile(rf"^\s*({_PSF_FLOAT})\s+({_PSF_FLOAT})\s*$", re.IGNORECASE)


def _unescape_psf_string(raw: str) -> str:
    return raw.replace('\\"', '"').replace("\\\\", "\\")


def _strip_prop_from_line(line: str, prop_depth: int) -> tuple[str, int]:
    """Strip balanced ``PROP(...)`` blocks across lines, ignoring quoted text."""
    out: list[str] = []
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


def _paren_delta(text: str) -> int:
    """Return net parenthesis depth change outside double-quoted strings."""
    depth = 0
    in_quotes = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if in_quotes:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_quotes = False
            continue
        if ch == '"':
            in_quotes = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth


def _skip_balanced_parens(
    lines: list[str],
    start_idx: int,
    end_idx: int,
    initial_text: str,
) -> int:
    """Advance past a balanced ``(...)`` block starting in *initial_text*."""
    depth = _paren_delta(initial_text)
    idx = start_idx + 1
    while depth > 0 and idx < end_idx:
        depth += _paren_delta(lines[idx])
        idx += 1
    return idx


def _advance_past_trailing_prop(
    lines: list[str],
    idx: int,
    end_idx: int,
    tail: str,
) -> int:
    prop_idx = tail.find("PROP(")
    if prop_idx != -1:
        return _skip_balanced_parens(lines, idx, end_idx, tail[prop_idx:])
    return idx + 1


def _read_balanced_group(
    lines: list[str],
    start_idx: int,
    end_idx: int,
    initial_rest: str,
) -> tuple[str, str, int]:
    """Read inside a ``(...)`` group starting after ``(``; return ``(body, trailing, close_idx)``."""
    body_parts: list[str] = []
    depth = 1
    idx = start_idx
    segment = initial_rest

    while True:
        in_quotes = False
        escaped = False
        closed_at: int | None = None
        for pos, ch in enumerate(segment):
            if escaped:
                escaped = False
                continue
            if in_quotes:
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_quotes = False
                continue
            if ch == '"':
                in_quotes = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    closed_at = pos
                    break
        if closed_at is not None:
            body_parts.append(segment[:closed_at])
            return "\n".join(body_parts), segment[closed_at + 1 :], idx
        body_parts.append(segment)
        idx += 1
        if idx >= end_idx:
            return "\n".join(body_parts), "", end_idx - 1
        segment = lines[idx]


def _tokenize_psf_struct_values(body: str) -> list[Any]:
    """Extract ordered scalar/complex/string members from a STRUCT body."""
    values: list[Any] = []
    pos = 0
    length = len(body)
    while pos < length:
        while pos < length and body[pos].isspace():
            pos += 1
        if pos >= length:
            break
        ch = body[pos]
        if ch == '"':
            end = pos + 1
            escaped = False
            while end < length:
                c = body[end]
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    break
                end += 1
            values.append(_unescape_psf_string(body[pos + 1 : end]))
            pos = end + 1
        elif ch == "(":
            depth = 1
            end = pos + 1
            in_quotes = False
            escaped = False
            while end < length and depth > 0:
                c = body[end]
                if escaped:
                    escaped = False
                elif in_quotes:
                    if c == "\\":
                        escaped = True
                    elif c == '"':
                        in_quotes = False
                elif c == '"':
                    in_quotes = True
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                end += 1
            inner = body[pos + 1 : end - 1 if depth == 0 else end]
            m_pair = _RE_PSF_COMPLEX_PAIR.match(inner)
            if m_pair:
                values.append(complex(float(m_pair.group(1)), float(m_pair.group(2))))
            else:
                values.append(body[pos:end].strip())
            pos = end
        else:
            end = pos
            while end < length and not body[end].isspace():
                end += 1
            values.append(_parse_psf_scalar(body[pos:end]))
            pos = end
    return values


def _parse_psf_non_swept_data(
    lines: list[str],
    n: int,
    sections: dict[str, int],
) -> dict[str, Any]:
    """Parse non-swept PSF ASCII data (e.g. operating-point info files)."""
    data: dict[str, Any] = {}

    struct_fields = _parse_psf_struct_types(
        lines,
        sections.get("TYPE", 0) + 1,
        sections.get("VALUE", n),
    )

    value_start = sections["VALUE"] + 1
    value_end = sections.get("END", n)

    i = value_start
    while i < value_end:
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped == "END":
            break
        if stripped.startswith("PROP("):
            i = _skip_balanced_parens(lines, i, value_end, stripped)
            continue

        # Typed parenthesized value: either a type-defined STRUCT or a typed complex tuple:
        #   "M0" "mos" ( <gm> <vth> ... ) PROP(...)
        #   "Z11" "Ohm" (50.0 -12.5) PROP(...)
        m_paren = _RE_PSF_TYPED_PAREN.match(stripped)
        if m_paren:
            name, type_name, rest = m_paren.groups()
            body, trailing, close_idx = _read_balanced_group(lines, i, value_end, rest)
            if type_name in struct_fields:
                values = _tokenize_psf_struct_values(body)
                for field, value in zip(struct_fields[type_name], values):
                    data[f"{name}:{field}"] = value
            else:
                m_complex = _RE_PSF_COMPLEX_PAIR.match(body)
                if m_complex:
                    data[name] = complex(float(m_complex.group(1)), float(m_complex.group(2)))
            i = _advance_past_trailing_prop(lines, close_idx, value_end, trailing)
            continue

        # Typed string value: "M0:model" "STRING" "nch" [PROP(...)]
        m_typed_str = _RE_PSF_TYPED_STR.match(stripped)
        if m_typed_str:
            name, _type_name, raw_str, tail = m_typed_str.groups()
            data[name] = _unescape_psf_string(raw_str)
            i = _advance_past_trailing_prop(lines, i, value_end, tail)
            continue

        # Typed numeric value (including nan/inf): "M0:gm" "S" 1.906e-04 [PROP(...)]
        m_typed_num = _RE_PSF_TYPED_NUM.match(stripped)
        if m_typed_num:
            name, _type_name, raw_num, tail = m_typed_num.groups()
            try:
                data[name] = float(raw_num)
            except ValueError:
                pass
            i = _advance_past_trailing_prop(lines, i, value_end, tail)
            continue

        # Typed unquoted non-numeric value: "M0:mode" "ENUM" sat [PROP(...)]
        m_typed_tok = _RE_PSF_TYPED_TOKEN.match(stripped)
        if m_typed_tok and not m_typed_tok.group(3).startswith("PROP("):
            name, _type_name, raw_tok, tail = m_typed_tok.groups()
            data[name] = _parse_psf_scalar(raw_tok)
            i = _advance_past_trailing_prop(lines, i, value_end, tail)
            continue

        # Untyped complex value: "VO" (1.0 -0.5) [PROP(...)]
        m_untyped_complex = _RE_PSF_UNTYPED_COMPLEX.match(stripped)
        if m_untyped_complex:
            name, real_s, imag_s, tail = m_untyped_complex.groups()
            data[name] = complex(float(real_s), float(imag_s))
            i = _advance_past_trailing_prop(lines, i, value_end, tail)
            continue

        # Untyped numeric value (including nan/inf): "name" numeric_value [PROP(...)]
        m_num = _RE_PSF_UNTYPED_NUM.match(stripped)
        if m_num:
            name, raw_num, tail = m_num.groups()
            try:
                data[name] = float(raw_num)
            except ValueError:
                pass
            i = _advance_past_trailing_prop(lines, i, value_end, tail)
            continue

        # Legacy untyped quoted string: "name" "value" [PROP(...)]
        m_str = _RE_PSF_UNTYPED_STR.match(stripped)
        if m_str:
            name, raw_str, tail = m_str.groups()
            data[name] = _unescape_psf_string(raw_str)
            i = _advance_past_trailing_prop(lines, i, value_end, tail)
            continue

        # Legacy untyped bare token: "name" token [PROP(...)]
        m = _RE_PSF_UNTYPED_TOKEN.match(stripped)
        if m:
            name, raw_value, tail = m.groups()
            if raw_value.startswith("PROP("):
                i = _skip_balanced_parens(lines, i, value_end, stripped[stripped.find("PROP(") :])
                continue
            try:
                data[name] = float(raw_value)
            except ValueError:
                data[name] = raw_value.strip('"')
            i = _advance_past_trailing_prop(lines, i, value_end, tail)
            continue

        i += 1

    return data


def _parse_psf_struct_types(lines: list[str], start: int, end: int) -> dict[str, list[str]]:
    """Return ordered member names for each STRUCT declared in a TYPE section."""
    structs: dict[str, list[str]] = {}
    current_type: str | None = None
    depth = 0
    prop_depth = 0

    for line in lines[start:end]:
        stripped = line.strip()
        if not stripped:
            continue
        if prop_depth or "PROP" in stripped:
            stripped, prop_depth = _strip_prop_from_line(stripped, prop_depth)
            if not stripped:
                continue
        if current_type is None:
            match = re.match(r'^"([^"]+)"\s+STRUCT\($', stripped)
            if match:
                current_type = match.group(1)
                structs[current_type] = []
                depth = 1
            continue

        # Members appear one level inside STRUCT; PROP metadata is already stripped.
        member = re.match(r'^"([^"]+)"\s+[A-Z]+(?:\s+[A-Z]+)?$', stripped)
        if depth == 1 and member:
            structs[current_type].append(member.group(1))

        depth += _paren_delta(stripped)
        if depth <= 0:
            current_type = None
            depth = 0

    return structs


def _parse_psf_scalar(raw_value: str) -> Any:
    """Parse one scalar from a STRUCT value while preserving non-numeric fields."""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    try:
        return float(value)
    except ValueError:
        return value

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
            or inner.joinpath("logFile").is_file()
        )
        if has_psf:
            return inner
    for child in sorted(raw_dir.iterdir()):
        if not child.is_dir():
            continue
        if any(child.glob("*.dc")) or any(child.glob("*.info")):
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
        for tran_file in sorted(output_dir.glob("*.tran.tran")):
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
        for ac_file in sorted(output_dir.glob("*.ac.ac")):
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
                sweep_data[point_idx] = point_data

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
            sweep_data[point_idx] = result.data

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
    for i in range(sweep_start, sweep_end):
        stripped = lines[i].strip()
        if not stripped or stripped in ("TRACE", "VALUE", "END"):
            break
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
    group_to_name: dict[str, str] = {}  # " 401" → "CLK_NET"
    current_group: str | None = None
    if "TRACE" in sections:
        trace_start = sections["TRACE"] + 1
        trace_end = sections.get("VALUE", n)
        for i in range(trace_start, trace_end):
            stripped = lines[i].strip()
            if not stripped or stripped in ("VALUE", "END"):
                break
            # GROUP marker: " N" GROUP 1
            m_group = re.match(r'"(\s*\d+)"\s+GROUP\s+\d+', stripped)
            if m_group:
                current_group = m_group.group(1)
                continue
            # Signal definition: "name" "V" or "name" "I" etc.
            m_sig = re.match(r'"([^"]+)"\s+"[^"]*"', stripped)
            if m_sig:
                sig_name = m_sig.group(1)
                trace_names.append(sig_name)
                if current_group is not None:
                    group_to_name[current_group] = sig_name
                current_group = None
                continue
            # Fallback: bare quoted name (no unit)
            m = re.match(r'"([^"]+)"', stripped)
            if m:
                trace_names.append(m.group(1))

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
    for i in range(value_start, value_end):
        stripped = lines[i].strip()
        if not stripped or stripped == "END":
            break
        # Complex value: "name" (real imag) → store as Python complex
        m_complex = re.match(r'"([^"]+)"\s+\(\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\)', stripped)
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
                for name in trace_names:
                    signal_series[name].append(signal_state.get(name, _MISSING))
            time_values.append(value)  # type: ignore[arg-type]
        else:
            signal_state[key] = value  # type: ignore[assignment]

    # Snapshot at the last time step
    if time_values:
        for name in trace_names:
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

def _parse_psf_non_swept_data(
    lines: list[str],
    n: int,
    sections: dict[str, int],
) -> dict[str, Any]:
    """Parse non-swept PSF ASCII data (e.g. operating-point info files)."""
    data: dict[str, Any] = {}

    value_start = sections["VALUE"] + 1
    value_end = sections.get("END", n)

    for i in range(value_start, value_end):
        stripped = lines[i].strip()
        if not stripped or stripped == "END":
            break

        # DC OP lines: "M0:gm" "S" 1.906e-04 PROP( ... )
        m_typed = re.match(
            r'^"([^"]+)"\s+(?:"[^"]+"\s+)([-+0-9.eE]+)',
            stripped,
        )
        if m_typed:
            try:
                data[m_typed.group(1)] = float(m_typed.group(2))
            except ValueError:
                pass
            continue

        # "name" numeric_value (no unit type token)
        m_num = re.match(r'^"([^"]+)"\s+([-+0-9.eE]+)', stripped)
        if m_num:
            try:
                data[m_num.group(1)] = float(m_num.group(2))
            except ValueError:
                pass
            continue

        # Legacy: "name" token (string or unquoted remainder)
        m = re.match(r'^"([^"]+)"\s+(\S+)', stripped)
        if m:
            name = m.group(1)
            raw_value = m.group(2)
            try:
                data[name] = float(raw_value)
            except ValueError:
                data[name] = raw_value.strip('"')

    return data

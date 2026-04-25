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
    """Parse swept PSF ASCII data (transient / DC sweep / AC)."""
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

    # Trace (dependent variable) names
    trace_names: list[str] = []
    if "TRACE" in sections:
        trace_start = sections["TRACE"] + 1
        trace_end = sections.get("VALUE", n)
        for i in range(trace_start, trace_end):
            stripped = lines[i].strip()
            if not stripped or stripped in ("VALUE", "END"):
                break
            m = re.match(r'"([^"]+)"', stripped)
            if m:
                trace_names.append(m.group(1))

    if not sweep_var:
        return {}

    data: dict[str, list[float | complex]] = {sweep_var: []}
    for name in trace_names:
        data[name] = []

    # Parse VALUE section
    value_start = sections["VALUE"] + 1
    value_end = sections.get("END", n)
    for i in range(value_start, value_end):
        stripped = lines[i].strip()
        if not stripped or stripped == "END":
            break
        # Complex value: "name" (real imag) → store as Python complex
        m_complex = re.match(r'"([^"]+)"\s+\(\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\)', stripped)
        if m_complex:
            sig_name = m_complex.group(1)
            try:
                real = float(m_complex.group(2))
                imag = float(m_complex.group(3))
                value = complex(real, imag)
            except ValueError:
                continue
            if sig_name in data:
                data[sig_name].append(value)
            continue
        # Scalar value: "name" value
        m = re.match(r'"([^"]+)"\s+(\S+)', stripped)
        if m:
            sig_name = m.group(1)
            try:
                value = float(m.group(2))
            except ValueError:
                continue
            if sig_name in data:
                data[sig_name].append(value)

    # Sanity-check lengths
    if data.get(sweep_var):
        expected = len(data[sweep_var])
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

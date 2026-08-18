"""Small, analysis-agnostic helpers for parsed Spectre PSF ASCII data."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .parsers import parse_spectre_psf_ascii


def read_psf_ascii(path: Path) -> dict[str, Any]:
    """Read one PSF ASCII result file or raise with its parser error."""
    result = parse_spectre_psf_ascii(path)
    if not result.ok:
        details = "; ".join(result.errors) if result.errors else "no data parsed"
        raise ValueError("cannot parse PSF ASCII {}: {}".format(path, details))
    return result.data


def result_file(raw_psf: Path, filename: str) -> Path:
    """Return the one required result file below an explicit raw PSF root."""
    if not raw_psf.is_dir():
        raise FileNotFoundError("raw PSF directory is absent: {}".format(raw_psf))
    matches = sorted(path for path in raw_psf.rglob(filename) if path.is_file())
    if len(matches) != 1:
        raise ValueError("expected exactly one {} below {}, found {}".format(filename, raw_psf, len(matches)))
    return matches[0]


def scalar(data: Mapping[str, Any], raw_key: str) -> float:
    """Return one exact, finite real scalar PSF value."""
    if raw_key not in data:
        raise ValueError("PSF lacks required raw key: {}".format(raw_key))
    value = data[raw_key]
    if isinstance(value, complex) or isinstance(value, (list, tuple)):
        raise ValueError("PSF key {} must be one real scalar".format(raw_key))
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("PSF key {} is not numeric".format(raw_key)) from error
    if not math.isfinite(result):
        raise ValueError("PSF key {} is non-finite".format(raw_key))
    return result


def vector(data: Mapping[str, Any], raw_key: str) -> list[complex]:
    """Return one exact, non-empty finite complex PSF vector."""
    if raw_key not in data:
        raise ValueError("PSF lacks required raw key: {}".format(raw_key))
    value = data[raw_key]
    if not isinstance(value, list) or not value:
        raise ValueError("PSF key {} must be a non-empty vector".format(raw_key))
    try:
        result = [complex(item) for item in value]
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("PSF key {} is not a numeric vector".format(raw_key)) from error
    if not all(math.isfinite(item.real) and math.isfinite(item.imag) for item in result):
        raise ValueError("PSF key {} has non-finite values".format(raw_key))
    return result


def frequency_hz(data: Mapping[str, Any], raw_key: str = "freq") -> list[float]:
    """Return one exact, strictly increasing real frequency vector in Hz."""
    frequencies_complex = vector(data, raw_key)
    if any(value.imag != 0.0 for value in frequencies_complex):
        raise ValueError("PSF frequency key {} must be real".format(raw_key))
    frequencies = [value.real for value in frequencies_complex]
    if any(right <= left for left, right in zip(frequencies, frequencies[1:])):
        raise ValueError("PSF frequency key {} is not strictly increasing".format(raw_key))
    return frequencies

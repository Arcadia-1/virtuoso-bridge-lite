"""Tests for small typed accessors over parsed Spectre PSF data."""

from __future__ import annotations

from pathlib import Path

import pytest

from virtuoso_bridge.spectre.psf import frequency_hz, result_file, scalar, vector


def test_scalar_requires_one_finite_real_value() -> None:
    assert scalar({"vout": 1.25}, "vout") == pytest.approx(1.25)

    with pytest.raises(ValueError, match="required raw key"):
        scalar({}, "vout")
    with pytest.raises(ValueError, match="real scalar"):
        scalar({"vout": 1 + 2j}, "vout")
    with pytest.raises(ValueError, match="real scalar"):
        scalar({"vout": [1.25]}, "vout")
    with pytest.raises(ValueError, match="non-finite"):
        scalar({"vout": float("nan")}, "vout")


def test_vector_and_frequency_hz_validate_shape_and_values() -> None:
    data = {"freq": [1.0e9, 2.0e9], "vout": [1.0, 1 + 2j]}

    assert vector(data, "vout") == [1 + 0j, 1 + 2j]
    assert frequency_hz(data) == [1.0e9, 2.0e9]

    with pytest.raises(ValueError, match="non-empty vector"):
        vector({"vout": []}, "vout")
    with pytest.raises(ValueError, match="non-finite"):
        vector({"vout": [complex(float("nan"), 0.0)]}, "vout")
    with pytest.raises(ValueError, match="must be real"):
        frequency_hz({"freq": [1.0e9 + 1j]})
    with pytest.raises(ValueError, match="strictly increasing"):
        frequency_hz({"freq": [2.0e9, 1.0e9]})


def test_result_file_requires_one_exact_match(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    nested = raw / "psf"
    nested.mkdir(parents=True)
    target = nested / "ac.ac"
    target.write_text("fixture", encoding="ascii")

    assert result_file(raw, "ac.ac") == target

    (raw / "another.ac").write_text("fixture", encoding="ascii")
    with pytest.raises(ValueError, match="expected exactly one"):
        result_file(raw, "*.ac")

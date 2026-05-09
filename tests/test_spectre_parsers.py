"""Regression tests for ``virtuoso_bridge.spectre.parsers`` and the
sweep-data wiring in ``_build_simulation_result``.

Covers behavior introduced in:
  * #76 (chenzc24): X/LX flat sweep layout, GROUP→signal mapping in
    ``_parse_psf_swept_data``, delta-compressed PSF handling, and the
    "Circuit read-in complete" false-positive fix in the runner.
  * #77 (follow-up): wiring ``parse_sweep_psf_directory`` into
    ``_build_simulation_result`` so sweep results surface via
    ``metadata["sweep_points"]``, and replacing the silent ``0.0``
    fallback with ``math.nan`` for late-appearing signals.

These tests synthesize PSF ASCII text and directory layouts on the
fly — no real Spectre install needed.  When real PSF samples become
available, drop them into ``tests/fixtures/`` and add corresponding
parametrized tests.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from virtuoso_bridge.spectre.parsers import (
    _parse_psf_swept_data,
    parse_sweep_psf_directory,
)
from virtuoso_bridge.spectre.runner import _build_simulation_result


# ---------------------------------------------------------------------------
# _parse_psf_swept_data — delta-compressed + late-appearing signals
# ---------------------------------------------------------------------------

PSF_TEXT_LATE_SIGNAL = """\
HEADER
PROPERTIES
SWEEP
"time" 1
TRACE
"sig_a" "V"
"sig_b" "V"
VALUE
"time" 0.0
"sig_a" 1.0
"time" 1e-9
"sig_a" 1.5
"sig_b" 0.5
"time" 2e-9
"sig_a" 2.0
"sig_b" 0.7
END
"""
PSF_SECTIONS_LATE_SIGNAL = {
    "HEADER": 0,
    "PROPERTIES": 1,
    "SWEEP": 2,
    "TRACE": 4,
    "VALUE": 7,
    "END": 16,
}


@pytest.fixture
def parsed_late_signal():
    lines = PSF_TEXT_LATE_SIGNAL.splitlines()
    return _parse_psf_swept_data(lines, len(lines), PSF_SECTIONS_LATE_SIGNAL)


def test_swept_time_vector(parsed_late_signal):
    assert parsed_late_signal["time"] == [0.0, 1e-9, 2e-9]


def test_swept_signal_always_present_unchanged(parsed_late_signal):
    assert parsed_late_signal["sig_a"] == [1.0, 1.5, 2.0]


def test_swept_signal_late_first_step_is_nan(parsed_late_signal):
    """Signal absent at the first time step must read NaN, not 0.0.

    Regression of PR #77: the old `signal_state.get(name, 0.0)`
    fallback was indistinguishable from a real 0V reading.
    """
    sig_b = parsed_late_signal["sig_b"]
    assert len(sig_b) == 3
    assert math.isnan(sig_b[0])
    assert sig_b[1] == 0.5
    assert sig_b[2] == 0.7


# ---------------------------------------------------------------------------
# parse_sweep_psf_directory — both layouts
# ---------------------------------------------------------------------------

_PSF_TEMPLATE = """\
HEADER
PROPERTIES
SWEEP
"time" 1
TRACE
"v_out" "V"
VALUE
"time" 0.0
"v_out" {a}
"time" 1e-9
"v_out" {b}
END
"""


@pytest.fixture
def sweep_dir_subdir(tmp_path):
    """Classic Spectre layout: ``sw1.sweep1/<idx>/<file>.tran.tran.tran``."""
    for pt in (1, 2, 3):
        d = tmp_path / "sw1.sweep1" / str(pt)
        d.mkdir(parents=True)
        (d / "tran.tran.tran").write_text(
            _PSF_TEMPLATE.format(a=pt * 0.5, b=pt * 1.0)
        )
    return tmp_path


@pytest.fixture
def sweep_dir_flat(tmp_path):
    """Spectre X/LX flat layout: ``sw1-NNN_<file>.tran.tran.tran``."""
    for raw_idx in (0, 1, 2):
        f = tmp_path / f"sw1-{raw_idx:03d}_tran.tran.tran"
        # Single-step PSF for compact fixture
        f.write_text(
            f"""HEADER
PROPERTIES
SWEEP
"time" 1
TRACE
"v_out" "V"
VALUE
"time" 0.0
"v_out" {(raw_idx + 1) * 0.3}
END
"""
        )
    return tmp_path


def test_sweep_subdir_layout_indexes(sweep_dir_subdir):
    sweep = parse_sweep_psf_directory(sweep_dir_subdir)
    assert sorted(sweep.keys()) == [1, 2, 3]


def test_sweep_subdir_layout_values(sweep_dir_subdir):
    sweep = parse_sweep_psf_directory(sweep_dir_subdir)
    # pt=2 → v_out values [2*0.5, 2*1.0] = [1.0, 2.0]
    assert sweep[2]["v_out"] == [1.0, 2.0]


def test_sweep_flat_layout_bumps_to_1_indexed(sweep_dir_flat):
    """Spectre X/LX uses 0-indexed raw filenames; the parser must
    convert to 1-indexed point numbers to match the classic
    ``sw1.sweep1/N/`` convention so sweep-aware consumers don't
    have to special-case the layout source."""
    sweep = parse_sweep_psf_directory(sweep_dir_flat)
    assert sorted(sweep.keys()) == [1, 2, 3]
    # raw_idx 0 → point 1, value (0+1)*0.3 = 0.3
    assert sweep[1]["v_out"] == [0.3]


def test_sweep_dir_no_layout_returns_empty(tmp_path):
    """Single-point output (no ``sw*`` prefix anywhere) returns an
    empty dict — caller falls back to ``parse_psf_ascii_directory``."""
    (tmp_path / "tran.tran.tran").write_text("HEADER\nEND\n")
    assert parse_sweep_psf_directory(tmp_path) == {}


# ---------------------------------------------------------------------------
# _build_simulation_result — runner.py wiring
# ---------------------------------------------------------------------------

def _fake_run_result(output_dir, *, returncode=0, stdout="", stderr=""):
    return SimpleNamespace(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        output_dir=output_dir,
        execution_time=0.1,
    )


def test_build_result_populates_sweep_points(sweep_dir_subdir):
    """PR #77: sweep results must surface via ``metadata['sweep_points']``."""
    run = _fake_run_result(
        sweep_dir_subdir,
        stdout="Circuit read-in complete\nsimulation completed.\n",
    )
    result = _build_simulation_result(
        run, output_format="psfascii", extra_metadata=None,
    )
    sweep_meta = result.metadata.get("sweep_points")
    assert sweep_meta is not None
    assert sorted(sweep_meta.keys()) == [1, 2, 3]


def test_build_result_no_readin_false_positive(sweep_dir_subdir):
    """PR #76: 'Circuit read-in complete' is normal Spectre output and
    must NOT produce a 'netlist read error' entry."""
    run = _fake_run_result(
        sweep_dir_subdir,
        stdout="Circuit read-in complete\nsimulation completed.\n",
    )
    result = _build_simulation_result(
        run, output_format="psfascii", extra_metadata=None,
    )
    err_text = " ".join(result.errors).lower()
    assert "netlist read error" not in err_text


def test_build_result_actual_readin_error_still_flagged(tmp_path):
    """Regression guard: an actual 'error reading' message must still
    be classified as a netlist read error."""
    run = _fake_run_result(
        tmp_path,
        stdout="error reading netlist: cannot resolve include\n",
        returncode=1,
    )
    result = _build_simulation_result(
        run, output_format="psfascii", extra_metadata=None,
    )
    err_text = " ".join(result.errors).lower()
    assert "netlist read error" in err_text

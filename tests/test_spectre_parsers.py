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
    _parse_psf_ascii_content,
    _parse_psf_swept_data,
    parse_psf_ascii_directory,
    parse_spectre_psf_ascii,
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


def test_swept_ac_signal_preserves_complex_phasor():
    """AC PSF values are phasors; consumers must choose magnitude/phase."""
    text = """\
HEADER
PROPERTIES
SWEEP
"freq" 1
TRACE
"VO" "V"
VALUE
"freq" 1e6
"VO" (1.0 0.0)
"freq" 1e9
"VO" (0.5 -0.5)
END
"""
    lines = text.splitlines()
    parsed = _parse_psf_swept_data(
        lines,
        len(lines),
        {"HEADER": 0, "PROPERTIES": 1, "SWEEP": 2, "TRACE": 4, "VALUE": 6, "END": 11},
    )

    assert parsed["freq"] == [1e6, 1e9]
    assert parsed["VO"] == [complex(1.0, 0.0), complex(0.5, -0.5)]


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
# parse_spectre_psf_ascii — STRUCT operating-point values
# ---------------------------------------------------------------------------

def test_non_swept_struct_values_are_flattened_by_instance_and_member(tmp_path):
    """Operating-point info stores device scalars as ordered STRUCT members."""
    psf_file = tmp_path / "finalTimeOP.info"
    psf_file.write_text("""\
HEADER
"analysis type" "info"
TYPE
"nmos" STRUCT(
"gm" FLOAT DOUBLE PROP(
"units" "S"
)
"vth" FLOAT DOUBLE PROP(
"units" "V"
)
"region" STRING PROP(
)
) PROP(
"key" "inst"
)
VALUE
"M0" "nmos" (
1.906e-04
4.500e-01
"saturation"
) PROP(
"model" "nmos"
)
END
""", encoding="utf-8")

    result = parse_spectre_psf_ascii(psf_file)

    assert result.ok
    assert result.data["M0:gm"] == pytest.approx(1.906e-04)
    assert result.data["M0:vth"] == pytest.approx(0.45)
    assert result.data["M0:region"] == "saturation"


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


def test_build_result_ignores_successful_convergence_chatter(tmp_path):
    run = _fake_run_result(
        tmp_path,
        stdout="No convergence difficulties encountered.\nsimulation completed.\n",
    )

    result = _build_simulation_result(run, output_format="psfascii")

    assert result.ok
    assert "convergence failure" not in result.errors


def test_build_result_marks_fatal_output_as_failure_even_with_zero_exit(tmp_path):
    run = _fake_run_result(
        tmp_path,
        stdout="ERROR (SFE-23): instance M0 is invalid\n",
    )

    result = _build_simulation_result(run, output_format="psfascii")

    assert not result.ok
    assert result.status.value == "failure"
    assert result.errors


def test_build_result_marks_pss_convergence_failure_with_zero_exit(tmp_path):
    run = _fake_run_result(
        tmp_path,
        stdout="SPCRTRF-15044: PSS analysis failed to converge\n",
    )

    result = _build_simulation_result(run, output_format="psfascii")

    assert not result.ok
    assert "convergence failure" in result.errors


# ---------------------------------------------------------------------------
# Issue #312 regressions: non-swept PROP/typed values, blank lines, globs, sweep merge
# ---------------------------------------------------------------------------

def test_non_swept_excludes_prop_and_parses_typed_string_nan_inf_complex():
    content = """\
HEADER
"analysis type" "info"
TYPE
"nmos" STRUCT(
"gm" FLOAT DOUBLE PROP(
"units" "S"
"desc" "gain (S)"
)
) PROP(
"key" "inst"
)
VALUE
"M0" "nmos" (1.9e-04) PROP("model" "nmos")

"M1" "nmos" (
2.5e-04
) PROP(
"model" "nmos_lvt"
"nested" (1 2)
)
"vout" "V" 1.23 PROP(
"key" "node"
)
"M0:model" "STRING" "nch"
"M0:note" "STRING" "quoted \\"note\\" text" PROP(
"ignore" "me"
)
"M0:ron" "Ohm" inf PROP("key" "param")
"M0:voff" "V" -inf
"M0:err" "V" nan
"Z11" "Ohm" (50.0 -12.5) PROP(
"unit" "Ohm"
)
"Y11" (0.02 0.005)
"legacy_str" "ok"
END
"""
    data = _parse_psf_ascii_content(content)

    assert data["M0:gm"] == pytest.approx(1.9e-04)
    assert data["M1:gm"] == pytest.approx(2.5e-04)
    assert data["vout"] == pytest.approx(1.23)
    assert data["M0:model"] == "nch"
    assert data["M0:note"] == 'quoted "note" text'
    assert math.isinf(data["M0:ron"]) and data["M0:ron"] > 0
    assert math.isinf(data["M0:voff"]) and data["M0:voff"] < 0
    assert math.isnan(data["M0:err"])
    assert data["Z11"] == complex(50.0, -12.5)
    assert data["Y11"] == complex(0.02, 0.005)
    assert data["legacy_str"] == "ok"
    for leaked in ("model", "key", "nested", "ignore", "unit"):
        assert leaked not in data


def test_swept_parser_survives_blank_lines_in_sweep_trace_and_value():
    content = """\
HEADER
SWEEP

"time" 1

TRACE

"sig_a" "V"

"sig_b" "V"

VALUE

"time" 0.0
"sig_a" 1.0

"time" 1e-9
"sig_a" 2.0
"sig_b" 0.5

END
"""
    data = _parse_psf_ascii_content(content)
    assert data["time"] == [0.0, 1e-9]
    assert data["sig_a"] == [1.0, 2.0]
    assert math.isnan(data["sig_b"][0])
    assert data["sig_b"][1] == 0.5


def test_parse_psf_ascii_directory_falls_back_to_custom_tran_and_ac(tmp_path):
    (tmp_path / "mytran.tran").write_text(_PSF_TEMPLATE.format(a=0.2, b=0.8), encoding="utf-8")
    (tmp_path / "myac.ac").write_text(
        """\
HEADER
SWEEP
"freq" 1
TRACE
"VO" "V"
VALUE
"freq" 1e6
"VO" (1.0 -0.5)
END
""",
        encoding="utf-8",
    )

    data = parse_psf_ascii_directory(tmp_path)
    assert data["time"] == [0.0, 1e-9]
    assert data["v_out"] == [0.2, 0.8]
    assert data["ac_freq"] == [1e6]
    assert data["ac_VO"] == [complex(1.0, -0.5)]


def test_parse_sweep_psf_directory_merges_multiple_flat_analyses_per_point(tmp_path):
    (tmp_path / "sw1-000_dc.dc").write_text(
        'HEADER\nVALUE\n"vdc" "V" 0.9\nEND\n',
        encoding="utf-8",
    )
    (tmp_path / "sw1-000_tran.tran.tran").write_text(
        _PSF_TEMPLATE.format(a=0.1, b=0.9),
        encoding="utf-8",
    )
    (tmp_path / "sw1-001_dc.dc").write_text(
        'HEADER\nVALUE\n"vdc" "V" 1.1\nEND\n',
        encoding="utf-8",
    )
    (tmp_path / "sw1-001_tran.tran.tran").write_text(
        _PSF_TEMPLATE.format(a=0.2, b=1.1),
        encoding="utf-8",
    )

    sweep = parse_sweep_psf_directory(tmp_path)
    assert sorted(sweep.keys()) == [1, 2]
    assert sweep[1]["vdc"] == pytest.approx(0.9)
    assert sweep[1]["v_out"] == [0.1, 0.9]
    assert sweep[2]["vdc"] == pytest.approx(1.1)
    assert sweep[2]["v_out"] == [0.2, 1.1]


# ---------------------------------------------------------------------------
# Issue #339 regressions: TRACE PROP(...) GROUP mapping & duplicate trace_names
# ---------------------------------------------------------------------------

def test_swept_trace_group_with_prop_does_not_steal_mapping_or_duplicate_traces():
    content = """\
HEADER
TYPE
"V" FLOAT DOUBLE PROP( "units" "V" )
SWEEP
"time" "time" PROP(
"sweep_direction" 0
)
TRACE
"group" GROUP 1 PROP(
"key" "node"
"nested" (1 (2 3))
)
"vout" "V" PROP( "key" "node" )
" 1" GROUP 1 PROP(
"key" "node"
)
"vin" "V" PROP(
"key" "node"
)
VALUE
"time" 0.0
"group" 1.2
" 1" 0.0
"time" 1e-9
"group" 0.3
" 1" 1.8
END
"""
    data = _parse_psf_ascii_content(content)

    assert set(data.keys()) == {"time", "vout", "vin"}
    assert data["time"] == [0.0, 1e-9]
    assert data["vout"] == [1.2, 0.3]
    assert data["vin"] == [0.0, 1.8]
    assert "key" not in data
    assert "group" not in data


def test_non_swept_struct_inline_prop_preserves_members():
    content = """\
HEADER
TYPE
"nmos" STRUCT(
"gm" FLOAT DOUBLE PROP( "units" "S" )
"vth" FLOAT DOUBLE PROP(
"units" "V"
)
) PROP( "key" "inst" )
VALUE
"M0" "nmos" (1.5e-4 0.42)
END
"""
    data = _parse_psf_ascii_content(content)
    assert data == {"M0:gm": pytest.approx(1.5e-4), "M0:vth": pytest.approx(0.42)}

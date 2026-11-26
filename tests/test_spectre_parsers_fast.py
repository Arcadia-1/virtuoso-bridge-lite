"""Data correctness regressions for the optional fast PSF parser."""
import math
import pytest
from virtuoso_bridge.spectre.parsers_fast import signal_stats


@pytest.mark.parametrize('values,rms', [([1, 1, 1], 1), ([-2, -2], 2),
    ([3, 4], math.sqrt(12.5)), ([-1, 1], 1), ([0], 0),
    ([True, None, float('nan'), 3, 4], math.sqrt(12.5))])
def test_rms_is_root_mean_square(values, rms):
    assert signal_stats(values)['rms'] == pytest.approx(rms)


def test_empty_stats():
    assert signal_stats([]) == {'n': 0, 'numeric': 0}


def test_synthetic_sweep_matches_existing_parser_and_filters(tmp_path):
    from virtuoso_bridge.spectre.parsers import parse_spectre_psf_ascii
    from virtuoso_bridge.spectre.parsers_fast import parse_file_fast
    path = tmp_path / 'synthetic.tran'
    path.write_text('''HEADER
TYPE
SWEEP
"time" "s"
TRACE
"out" "V"
"in" "V"
VALUE
"time" 0
"out" 1
"in" 0
"time" 1
"out" 2
"in" 3
END
''')
    full = parse_file_fast(path)
    assert full == parse_spectre_psf_ascii(path).data
    assert full == {'time': [0.0, 1.0], 'out': [1.0, 2.0], 'in': [0.0, 3.0]}
    assert parse_file_fast(path, want=['out']) == {'time': [0.0, 1.0], 'out': [1.0, 2.0]}


@pytest.mark.parametrize('tran_name', ['tran.tran', 'tran.tran.tran'])
@pytest.mark.parametrize('ac_name', ['ac.ac', 'ac.ac.ac'])
def test_directory_retains_all_analyses_without_parsing_twice(tmp_path, monkeypatch,
                                                           tran_name, ac_name):
    from virtuoso_bridge.spectre import parsers_fast
    from virtuoso_bridge.spectre.parsers import parse_psf_ascii_directory

    def write_sweep(name, axis, first, last):
        (tmp_path / name).write_text(
            'HEADER\nTYPE\nSWEEP\n"%s" "s"\nTRACE\n"out" "V"\nVALUE\n'
            '"%s" 0\n"out" %s\n"%s" 1\n"out" %s\nEND\n'
            % (axis, axis, first, axis, last), encoding='utf-8')

    write_sweep(tran_name, 'time', 1, 2)
    write_sweep(ac_name, 'freq', '(3 4)', '(5 6)')
    write_sweep('dc.dc', 'voltage', 7, 8)
    parsed = []
    parse = parsers_fast.parse_file_fast

    def record(path):
        parsed.append(path.name)
        return parse(path)

    monkeypatch.setattr(parsers_fast, 'parse_file_fast', record)
    result = parsers_fast.parse_dir_fast(tmp_path)
    assert result == {
        'time': [0.0, 1.0], 'out': [1.0, 2.0],
        'ac_freq': [0.0, 1.0], 'ac_out': [3+4j, 5+6j],
        'dc_voltage': [0.0, 1.0], 'dc_out': [7.0, 8.0],
    }
    assert result == parse_psf_ascii_directory(tmp_path)
    assert sorted(parsed) == sorted([tran_name, ac_name, 'dc.dc'])


def test_directory_selects_only_one_transient_candidate(tmp_path, monkeypatch):
    from virtuoso_bridge.spectre import parsers_fast
    for name in ('a.tran', 'b.tran.tran'):
        (tmp_path / name).touch()
    parsed = []

    def parse(path):
        parsed.append(path.name)
        return {'time': [0.0]}

    monkeypatch.setattr(parsers_fast, 'parse_file_fast', parse)
    assert parsers_fast.parse_dir_fast(tmp_path) == {'time': [0.0]}
    assert parsed == ['b.tran.tran']


def test_empty_directory(tmp_path):
    from virtuoso_bridge.spectre.parsers_fast import parse_dir_fast
    assert parse_dir_fast(tmp_path) == {}


def test_trace_group_with_prop_blocks_binds_real_signal_not_key(tmp_path):
    from virtuoso_bridge.spectre.parsers import parse_spectre_psf_ascii
    from virtuoso_bridge.spectre.parsers_fast import parse_file_fast

    psf = tmp_path / "tran.tran"
    psf.write_text(
        """\
HEADER
"PSFversion" "1.00"
TYPE
"V" FLOAT DOUBLE PROP(
"units" "V"
)
SWEEP
"time" "time" PROP(
"sweep_direction" 0
)
TRACE
"group" GROUP 1 PROP(
"key" "node"
"nested" (1 2)
"paren_in_str" "gain ) test"
)
"vout" "V" PROP( "key" "node" )
" 2" GROUP 1
PROP(
"key" "node"
)
"vin" "V" PROP(
"key" "node"
)
VALUE
"time" 0.0
"group" 0.9
" 2" 0.1
"time" 1e-9
"group" 1.8
" 2" 0.2
END
""",
        encoding="utf-8",
    )

    fast = parse_file_fast(psf)
    full = parse_spectre_psf_ascii(psf).data
    expected = {
        "time": [0.0, 1e-9],
        "vout": [0.9, 1.8],
        "vin": [0.1, 0.2],
    }
    assert fast == expected
    assert full == expected
    assert "key" not in fast
    assert "group" not in fast


def test_parse_dir_fast_returns_both_dcop_scalar_and_dcsweep(tmp_path):
    from virtuoso_bridge.spectre.parsers_fast import parse_dir_fast, parse_file_fast

    dcop = tmp_path / "dcOp.dc"
    dcop.write_text(
        """\
HEADER
"analysis type" "dc"
TYPE
"V" FLOAT DOUBLE PROP(
"units" "V"
)
VALUE
"vdd" "V" 1.8 PROP(
"key" "node"
)
"vout" "V" 0.9
"i_bias" -1.2e-5
END
""",
        encoding="utf-8",
    )
    dcsweep = tmp_path / "dcSweep.dc"
    dcsweep.write_text(
        """\
HEADER
TYPE
SWEEP
"vin" "V"
TRACE
"vout" "V"
VALUE
"vin" 0.0
"vout" 1.8
"vin" 0.9
"vout" 0.9
"vin" 1.8
"vout" 0.0
END
""",
        encoding="utf-8",
    )

    assert parse_file_fast(dcop) == {
        "vdd": pytest.approx(1.8),
        "vout": pytest.approx(0.9),
        "i_bias": pytest.approx(-1.2e-5),
    }
    assert parse_file_fast(dcop, want=["vout"]) == {"vout": pytest.approx(0.9)}

    merged = parse_dir_fast(tmp_path)
    assert merged["dc_vdd"] == pytest.approx(1.8)
    assert merged["dc_vout"] == pytest.approx(0.9)
    assert merged["dc_i_bias"] == pytest.approx(-1.2e-5)
    assert merged["dcSweep_vin"] == [0.0, 0.9, 1.8]
    assert merged["dcSweep_vout"] == [1.8, 0.9, 0.0]


def test_parse_file_fast_complex_nan_and_inf_do_not_carry_forward(tmp_path):
    from virtuoso_bridge.spectre.parsers import parse_spectre_psf_ascii
    from virtuoso_bridge.spectre.parsers_fast import parse_file_fast

    ac = tmp_path / "ac.ac"
    ac.write_text(
        """\
HEADER
SWEEP
"freq" "Hz"
TRACE
"VO" "V"
VALUE
"freq" 1e6
"VO" (1.0 -0.5)
"freq" 1e7
"VO" (NaN 0.0)
"freq" 1e8
"VO" (inf -Inf)
"freq" 1e9
"VO" (+INF -infinity)
END
""",
        encoding="utf-8",
    )

    for res in (parse_file_fast(ac), parse_spectre_psf_ascii(ac).data):
        assert res["freq"] == [1e6, 1e7, 1e8, 1e9]
        vo = res["VO"]
        assert len(vo) == 4
        assert vo[0] == complex(1.0, -0.5)
        assert math.isnan(vo[1].real) and vo[1].imag == 0.0
        assert math.isinf(vo[2].real) and vo[2].real > 0
        assert math.isinf(vo[2].imag) and vo[2].imag < 0
        assert math.isinf(vo[3].real) and vo[3].real > 0
        assert math.isinf(vo[3].imag) and vo[3].imag < 0


def test_signal_stats_excludes_infinities():
    st = signal_stats([float("-inf"), 1.0, float("inf"), float("nan"), 3.0, True])
    assert st["n"] == 6
    assert st["numeric"] == 2
    assert st["min"] == 1.0
    assert st["max"] == 3.0
    assert st["mean"] == pytest.approx(2.0)
    assert st["rms"] == pytest.approx(math.sqrt(5.0))
    assert st["first"] == 1.0
    assert st["last"] == 3.0
    assert signal_stats([float("inf"), float("-inf")]) == {"n": 2, "numeric": 0}


def test_measure_period_guards_duplicate_timestamps_and_non_finite():
    from virtuoso_bridge.spectre.parsers_fast import measure_period

    # Duplicate timestamps across threshold crossings must never raise ZeroDivisionError.
    assert measure_period([0.0, 1e-9, 1e-9, 2e-9], [0.0, 0.0, 1.0, 1.0]) == {}
    assert measure_period([0.0, 1e-9, 1e-9, 1e-9], [0.0, 1.0, 0.0, 1.0]) == {}
    assert measure_period([1e-9, 1e-9, 1e-9, 1e-9], [0.0, 1.0, 0.0, 1.0]) == {}

    # Duplicate timestamps at a crossing followed by a genuine second edge still measures cleanly.
    res = measure_period(
        [0.0, 1e-9, 1e-9, 1e-9, 2e-9, 3e-9],
        [0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
    )
    assert res["edges"] == 2
    assert res["period_s"] == pytest.approx(2e-9)
    assert res["freq_hz"] == pytest.approx(5e8)

    # Ideal step edges with duplicate timestamps per transition.
    step_res = measure_period(
        [0.0, 1e-9, 1e-9, 2e-9, 2e-9, 3e-9, 3e-9],
        [0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0],
    )
    assert step_res["edges"] == 2
    assert step_res["period_s"] == pytest.approx(2e-9)

    # Non-finite values and non-finite level are ignored safely.
    assert measure_period([0.0, 1e-9, 2e-9, 3e-9], [0.0, 1.0, 0.0, 1.0], level=float("nan")) == {}
    finite_res = measure_period(
        [0.0, float("inf"), 1e-9, 2e-9, 3e-9, 4e-9],
        [0.0, 0.5, 1.0, 0.0, 1.0, 0.0],
    )
    assert finite_res["period_s"] == pytest.approx(2e-9)

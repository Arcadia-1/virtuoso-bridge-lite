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

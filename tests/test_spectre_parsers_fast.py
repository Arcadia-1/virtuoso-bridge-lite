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

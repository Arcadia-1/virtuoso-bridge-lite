import math

import pytest

from analog_opt.stability import StabilityError, extract_stability_metrics


def response(magnitudes_db, phases_deg):
    return [
        10 ** (magnitude / 20.0) * complex(
            math.cos(math.radians(phase)), math.sin(math.radians(phase))
        )
        for magnitude, phase in zip(magnitudes_db, phases_deg)
    ]


def test_stability_metrics_interpolate_unity_and_phase_crossings():
    frequencies = [1e3, 1e4, 1e5, 1e6]
    loop_gain = response([40, 20, 0, -20], [-90, -110, -120, -200])
    metrics = extract_stability_metrics('stability', 'loop', frequencies, loop_gain)
    assert metrics['stb.stability.loop.phase_margin_deg'] == pytest.approx(60.0)
    assert metrics['stb.stability.loop.gain_margin_db'] == pytest.approx(15.0)
    assert metrics['stb.stability.loop.unity_loop_frequency_hz'] == pytest.approx(1e5)
    assert metrics['stb.stability.loop.low_frequency_loop_gain_db'] == pytest.approx(40.0)


def test_unity_crossing_uses_log_frequency_interpolation():
    frequencies = [1e3, 1e4, 1e5]
    loop_gain = response([20, 10, -10], [-100, -120, -140])
    metrics = extract_stability_metrics(
        'stability', 'loop', frequencies, loop_gain, require_gain_margin=False
    )
    assert metrics['stb.stability.loop.unity_loop_frequency_hz'] == pytest.approx(
        math.sqrt(1e4 * 1e5)
    )
    assert metrics['stb.stability.loop.phase_margin_deg'] == pytest.approx(50.0)
    assert 'stb.stability.loop.gain_margin_db' not in metrics


def test_missing_unity_crossing_is_not_a_number():
    with pytest.raises(StabilityError, match='unity crossing'):
        extract_stability_metrics(
            'stability', 'loop', [1, 10, 100],
            response([20, 10, 5], [-90, -100, -110]),
        )


def test_multiple_unity_crossings_require_explicit_policy():
    frequencies = [1, 10, 100, 1000]
    loop_gain = response([10, -10, 10, -10], [-100, -120, -140, -160])
    with pytest.raises(StabilityError, match='ambiguous unity crossing'):
        extract_stability_metrics(
            'stability', 'loop', frequencies, loop_gain,
            require_gain_margin=False,
        )
    first = extract_stability_metrics(
        'stability', 'loop', frequencies, loop_gain,
        crossing_policy='first', require_gain_margin=False,
    )
    last = extract_stability_metrics(
        'stability', 'loop', frequencies, loop_gain,
        crossing_policy='last', require_gain_margin=False,
    )
    assert first['stb.stability.loop.unity_loop_frequency_hz'] < last[
        'stb.stability.loop.unity_loop_frequency_hz'
    ]


def test_required_gain_margin_rejects_missing_phase_crossing():
    with pytest.raises(StabilityError, match='phase crossing'):
        extract_stability_metrics(
            'stability', 'loop', [1, 10, 100],
            response([20, 0, -20], [-90, -120, -150]),
        )


@pytest.mark.parametrize('frequencies,loop_gain', [
    ([1, 1, 10], response([20, 0, -20], [-90, -120, -180])),
    ([1, 10, 100], [10+0j, complex(math.nan, 0), .1+0j]),
])
def test_invalid_stability_curves_are_rejected(frequencies, loop_gain):
    with pytest.raises(StabilityError, match='curve'):
        extract_stability_metrics('stability', 'loop', frequencies, loop_gain)

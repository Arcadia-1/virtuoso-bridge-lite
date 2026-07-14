import pytest

from analog_opt.slew import SlewError, extract_closed_loop_slew


def pulse_waveform(step=0.25e-6):
    times = [index * step for index in range(int(10e-6 / step) + 1)]
    values = []
    for time in times:
        if time < 1e-6:
            value = 0.7
        elif time < 3e-6:
            value = 0.7 + (time - 1e-6) * 2e5
        elif time < 6e-6:
            value = 1.1
        elif time < 8e-6:
            value = 1.1 - (time - 6e-6) * 2e5
        else:
            value = 0.7
        values.append(value)
    return times, values


def test_closed_loop_slew_fits_twenty_to_eighty_percent():
    times, values = pulse_waveform()
    result = extract_closed_loop_slew(
        'closed_loop_slew', 'step', 'VOUT', times, values,
        low=0.7, high=1.1, fractions=(0.2, 0.8),
        rise_reference_time=1e-6, fall_reference_time=6e-6,
    )
    prefix = 'tran.closed_loop_slew.step.VOUT.'
    assert result.metrics[prefix + 'slew_rise_v_per_s'] == pytest.approx(2e5)
    assert result.metrics[prefix + 'slew_fall_v_per_s'] == pytest.approx(2e5)
    assert result.metrics[prefix + 'rise_delay_s'] == pytest.approx(1e-6)
    assert result.metrics[prefix + 'fall_delay_s'] == pytest.approx(1e-6)
    assert result.evidence['rise']['sample_count'] >= 3
    assert result.evidence['fall']['sample_count'] >= 3
    assert result.evidence['rise']['fit_residual_rms_v'] < 1e-12


def test_clipped_transition_is_rejected():
    times, values = pulse_waveform()
    clipped = [min(value, 0.95) for value in values]
    with pytest.raises(SlewError, match='clipped'):
        extract_closed_loop_slew(
            'closed_loop_slew', 'step', 'VOUT', times, clipped,
            low=0.7, high=1.1,
        )


def test_nonsettling_high_plateau_is_rejected():
    times, values = pulse_waveform()
    for index, time in enumerate(times):
        if 3e-6 <= time < 6e-6:
            values[index] = 1.1 + (0.03 if index % 2 else -0.03)
    with pytest.raises(SlewError, match='settle'):
        extract_closed_loop_slew(
            'closed_loop_slew', 'step', 'VOUT', times, values,
            low=0.7, high=1.1, settling_tolerance=0.02,
        )


def test_excessively_nonmonotonic_transition_is_rejected():
    times, values = pulse_waveform()
    transition = [index for index, time in enumerate(times) if 1.5e-6 <= time <= 2.5e-6]
    for offset, index in enumerate(transition):
        values[index] += 0.08 if offset % 2 else -0.08
    with pytest.raises(SlewError, match='non-monotonic'):
        extract_closed_loop_slew(
            'closed_loop_slew', 'step', 'VOUT', times, values,
            low=0.7, high=1.1, max_nonmonotonic_fraction=0.05,
        )


def test_insufficient_transition_samples_are_rejected():
    times, values = pulse_waveform(step=1e-6)
    with pytest.raises(SlewError, match='samples'):
        extract_closed_loop_slew(
            'closed_loop_slew', 'step', 'VOUT', times, values,
            low=0.7, high=1.1, min_fit_samples=4,
        )


@pytest.mark.parametrize('low,high,fractions', [
    (1.1, 0.7, (0.2, 0.8)),
    (0.7, 1.1, (0.8, 0.2)),
    (0.7, 1.1, (0.0, 0.8)),
])
def test_invalid_slew_configuration_is_rejected(low, high, fractions):
    times, values = pulse_waveform()
    with pytest.raises(SlewError, match='configuration'):
        extract_closed_loop_slew(
            'closed_loop_slew', 'step', 'VOUT', times, values,
            low=low, high=high, fractions=fractions,
        )

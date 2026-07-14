'''Closed-loop large-signal slew measurements for analog verification.'''

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


class SlewError(ValueError):
    '''Raised when a transient cannot prove standard closed-loop slew.'''


@dataclass(frozen=True)
class SlewResult:
    metrics: Mapping[str, float]
    evidence: Mapping[str, Any]


_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _name(value: str, label: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise SlewError('slew configuration has invalid ' + label)
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SlewError('slew configuration has invalid ' + label)
    parsed = float(value)
    if not math.isfinite(parsed):
        raise SlewError('slew configuration has invalid ' + label)
    return parsed


def _curve(times: Sequence[float], values: Sequence[float]) -> Tuple[list, list]:
    if not isinstance(times, (list, tuple)) or not isinstance(values, (list, tuple)):
        raise SlewError('slew curve must use finite sequences')
    if len(times) < 5 or len(times) != len(values):
        raise SlewError('slew curve lengths are invalid')
    xs = []
    ys = []
    for index, raw_time in enumerate(times):
        time = _finite(raw_time, 'time')
        value = _finite(values[index], 'value')
        if xs and time <= xs[-1]:
            raise SlewError('slew curve times must increase')
        xs.append(time)
        ys.append(value)
    return xs, ys


def _crossing(
    times: Sequence[float], values: Sequence[float], target: float,
    start_index: int, direction: int,
) -> Tuple[float, int]:
    for right in range(max(1, start_index), len(values)):
        left_value = values[right - 1]
        right_value = values[right]
        if direction > 0 and left_value <= target <= right_value and right_value > left_value:
            fraction = (target - left_value) / (right_value - left_value)
            return times[right - 1] + fraction * (times[right] - times[right - 1]), right
        if direction < 0 and left_value >= target >= right_value and right_value < left_value:
            fraction = (left_value - target) / (left_value - right_value)
            return times[right - 1] + fraction * (times[right] - times[right - 1]), right
    raise SlewError('closed-loop transition is clipped or absent')


def _fit_points(
    times: Sequence[float], values: Sequence[float],
    start_time: float, start_value: float, stop_time: float, stop_value: float,
) -> Tuple[list, list]:
    fit_times = [start_time]
    fit_values = [start_value]
    for time, value in zip(times, values):
        if start_time < time < stop_time:
            fit_times.append(time)
            fit_values.append(value)
    fit_times.append(stop_time)
    fit_values.append(stop_value)
    return fit_times, fit_values


def _least_squares(times: Sequence[float], values: Sequence[float]) -> Tuple[float, float]:
    center_time = sum(times) / len(times)
    center_value = sum(values) / len(values)
    denominator = sum((time - center_time) ** 2 for time in times)
    if denominator <= 0:
        raise SlewError('closed-loop transition has insufficient samples')
    slope = sum(
        (time - center_time) * (value - center_value)
        for time, value in zip(times, values)
    ) / denominator
    intercept = center_value - slope * center_time
    residual = math.sqrt(sum(
        (value - (slope * time + intercept)) ** 2
        for time, value in zip(times, values)
    ) / len(times))
    return slope, residual


def _reverse_fraction(values: Sequence[float], direction: int, swing: float) -> float:
    reverse = 0.0
    for left, right in zip(values, values[1:]):
        delta = right - left
        if direction > 0 and delta < 0:
            reverse += -delta
        elif direction < 0 and delta > 0:
            reverse += delta
    return reverse / swing


def _last_stable_run(
    times: Sequence[float], values: Sequence[float], target: float,
    tolerance: float, start_index: int, stop_index: int,
) -> Tuple[float, int]:
    runs = []
    run_start = None
    for index in range(start_index, stop_index + 1):
        inside = abs(values[index] - target) <= tolerance
        if inside and run_start is None:
            run_start = index
        if not inside and run_start is not None:
            runs.append((run_start, index - 1))
            run_start = None
    if run_start is not None:
        runs.append((run_start, stop_index))
    if not runs or runs[-1][1] - runs[-1][0] + 1 < 2:
        raise SlewError('closed-loop output does not settle')
    start, _ = runs[-1]
    return times[start], start


def extract_closed_loop_slew(
    profile_id: str,
    analysis_name: str,
    signal: str,
    times: Sequence[float],
    values: Sequence[float],
    *,
    low: float,
    high: float,
    fractions: Tuple[float, float] = (0.2, 0.8),
    settling_tolerance: float = 0.02,
    max_nonmonotonic_fraction: float = 0.1,
    min_fit_samples: int = 3,
    rise_reference_time: Optional[float] = None,
    fall_reference_time: Optional[float] = None,
) -> SlewResult:
    '''Measure positive and negative 20-80 percent closed-loop slew.'''
    profile = _name(profile_id, 'profile_id')
    analysis = _name(analysis_name, 'analysis_name')
    output = _name(signal, 'signal')
    xs, ys = _curve(times, values)
    low_value = _finite(low, 'low')
    high_value = _finite(high, 'high')
    if high_value <= low_value or not isinstance(fractions, (list, tuple)) or len(fractions) != 2:
        raise SlewError('slew configuration requires low < high and two fractions')
    lower_fraction = _finite(fractions[0], 'lower fraction')
    upper_fraction = _finite(fractions[1], 'upper fraction')
    tolerance_fraction = _finite(settling_tolerance, 'settling tolerance')
    reverse_limit = _finite(max_nonmonotonic_fraction, 'non-monotonic limit')
    if not 0 < lower_fraction < upper_fraction < 1 or tolerance_fraction <= 0 or reverse_limit < 0:
        raise SlewError('slew configuration fractions or tolerances are invalid')
    if type(min_fit_samples) is not int or min_fit_samples < 2:
        raise SlewError('slew configuration min_fit_samples is invalid')
    swing = high_value - low_value
    low_threshold = low_value + lower_fraction * swing
    high_threshold = low_value + upper_fraction * swing
    rise_start, rise_start_index = _crossing(xs, ys, low_threshold, 1, 1)
    rise_stop, rise_stop_index = _crossing(xs, ys, high_threshold, rise_start_index, 1)
    fall_start, fall_start_index = _crossing(xs, ys, high_threshold, rise_stop_index + 1, -1)
    fall_stop, fall_stop_index = _crossing(xs, ys, low_threshold, fall_start_index, -1)
    rise_mid, _ = _crossing(xs, ys, low_value + 0.5 * swing, rise_start_index, 1)
    fall_mid, fall_mid_index = _crossing(xs, ys, low_value + 0.5 * swing, fall_start_index, -1)
    rise_times, rise_values = _fit_points(xs, ys, rise_start, low_threshold, rise_stop, high_threshold)
    fall_times, fall_values = _fit_points(xs, ys, fall_start, high_threshold, fall_stop, low_threshold)
    if len(rise_times) < min_fit_samples or len(fall_times) < min_fit_samples:
        raise SlewError('closed-loop transition has insufficient samples')
    if _reverse_fraction(rise_values, 1, swing) > reverse_limit or _reverse_fraction(fall_values, -1, swing) > reverse_limit:
        raise SlewError('closed-loop transition is excessively non-monotonic')
    rise_slope, rise_residual = _least_squares(rise_times, rise_values)
    fall_slope, fall_residual = _least_squares(fall_times, fall_values)
    if rise_slope <= 0 or fall_slope >= 0:
        raise SlewError('closed-loop slew direction is invalid')
    tolerance = tolerance_fraction * swing
    high_settle, _ = _last_stable_run(xs, ys, high_value, tolerance, rise_stop_index, fall_mid_index)
    low_settle, _ = _last_stable_run(xs, ys, low_value, tolerance, fall_stop_index, len(xs) - 1)
    prefix = 'tran.%s.%s.%s.' % (profile, analysis, output)
    metrics = {
        prefix + 'slew_rise_v_per_s': float(rise_slope),
        prefix + 'slew_fall_v_per_s': float(-fall_slope),
        prefix + 'rise_settling_time_s': float(high_settle - (rise_reference_time if rise_reference_time is not None else rise_mid)),
        prefix + 'fall_settling_time_s': float(low_settle - (fall_reference_time if fall_reference_time is not None else fall_mid)),
        prefix + 'overshoot_v': float(max(0.0, max(ys[rise_stop_index:fall_mid_index + 1]) - high_value)),
        prefix + 'undershoot_v': float(max(0.0, low_value - min(ys[fall_mid_index:]))),
        prefix + 'final_error_v': float(abs(ys[-1] - low_value)),
    }
    if rise_reference_time is not None:
        metrics[prefix + 'rise_delay_s'] = float(rise_mid - _finite(rise_reference_time, 'rise reference time'))
    if fall_reference_time is not None:
        metrics[prefix + 'fall_delay_s'] = float(fall_mid - _finite(fall_reference_time, 'fall reference time'))
    if any(not math.isfinite(value) or value < 0 for value in metrics.values()):
        raise SlewError('closed-loop slew metrics must be finite and nonnegative')
    evidence: Dict[str, Any] = {
        'method': 'least_squares_20_80',
        'thresholds_v': {'low': low_threshold, 'high': high_threshold},
        'rise': {
            'interval_s': [rise_start, rise_stop],
            'sample_count': len(rise_times),
            'fit_residual_rms_v': rise_residual,
        },
        'fall': {
            'interval_s': [fall_start, fall_stop],
            'sample_count': len(fall_times),
            'fit_residual_rms_v': fall_residual,
        },
    }
    return SlewResult(metrics=metrics, evidence=evidence)

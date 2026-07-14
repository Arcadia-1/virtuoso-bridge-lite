'''Loop-stability measurements from fresh complex Spectre STB data.'''

from __future__ import annotations

import math
import re
from typing import Dict, Sequence, Tuple


class StabilityError(ValueError):
    '''Raised when loop-gain data cannot prove a stability metric.'''


_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_CROSSING_POLICIES = {'single', 'first', 'last'}


def _validate_name(value: str, label: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise StabilityError(label + ' must be a safe identifier')
    return value


def _validate_curve(
    frequencies: Sequence[float], response: Sequence[complex]
) -> Tuple[list, list]:
    if not isinstance(frequencies, (list, tuple)) or not isinstance(response, (list, tuple)):
        raise StabilityError('stability curve must use finite sequences')
    if len(frequencies) < 2 or len(frequencies) != len(response):
        raise StabilityError('stability curve lengths are invalid')
    xs = []
    ys = []
    for index, value in enumerate(frequencies):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StabilityError('stability curve frequency is invalid')
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0 or (xs and parsed <= xs[-1]):
            raise StabilityError('stability curve frequencies must increase')
        xs.append(parsed)
        item = response[index]
        if not isinstance(item, complex):
            raise StabilityError('stability curve response must be complex')
        if not math.isfinite(item.real) or not math.isfinite(item.imag) or abs(item) <= 0:
            raise StabilityError('stability curve response must be finite')
        ys.append(item)
    return xs, ys


def _unwrap_phase(response: Sequence[complex]) -> list:
    raw = [math.degrees(math.atan2(value.imag, value.real)) for value in response]
    unwrapped = [raw[0]]
    for value in raw[1:]:
        while value - unwrapped[-1] > 180.0:
            value -= 360.0
        while value - unwrapped[-1] < -180.0:
            value += 360.0
        unwrapped.append(value)
    return unwrapped


def _crossings(frequencies: Sequence[float], values: Sequence[float], target: float) -> list:
    found = []
    for index, value in enumerate(values):
        current_is_target = math.isclose(value, target, rel_tol=0.0, abs_tol=1e-12)
        if current_is_target:
            found.append((frequencies[index], index, index, 0.0))
        if index + 1 >= len(values):
            continue
        next_value = values[index + 1]
        next_is_target = math.isclose(next_value, target, rel_tol=0.0, abs_tol=1e-12)
        if current_is_target or next_is_target:
            continue
        if (value - target) * (next_value - target) < 0:
            fraction = (target - value) / (next_value - value)
            log_frequency = math.log10(frequencies[index]) + fraction * (
                math.log10(frequencies[index + 1]) - math.log10(frequencies[index])
            )
            found.append((10 ** log_frequency, index, index + 1, fraction))
    return found


def _select_crossing(crossings: list, policy: str, label: str) -> tuple:
    if policy not in _CROSSING_POLICIES:
        raise StabilityError('crossing_policy must be single, first, or last')
    if not crossings:
        raise StabilityError(label + ' crossing is missing')
    if len(crossings) > 1 and policy == 'single':
        raise StabilityError('ambiguous ' + label + ' crossing')
    return crossings[-1] if policy == 'last' else crossings[0]


def _interpolate(values: Sequence[float], crossing: tuple) -> float:
    _, left, right, fraction = crossing
    if left == right:
        return float(values[left])
    return float(values[left] + fraction * (values[right] - values[left]))


def extract_stability_metrics(
    profile_id: str,
    analysis_name: str,
    frequencies: Sequence[float],
    response: Sequence[complex],
    *,
    crossing_policy: str = 'single',
    require_gain_margin: bool = True,
) -> Dict[str, float]:
    '''Return finite, profile-qualified metrics from one STB loop-gain curve.'''
    profile = _validate_name(profile_id, 'profile_id')
    analysis = _validate_name(analysis_name, 'analysis_name')
    xs, ys = _validate_curve(frequencies, response)
    magnitude_db = [20.0 * math.log10(abs(value)) for value in ys]
    phase_deg = _unwrap_phase(ys)
    unity = _select_crossing(_crossings(xs, magnitude_db, 0.0), crossing_policy, 'unity')
    phase_at_unity = _interpolate(phase_deg, unity)
    prefix = 'stb.%s.%s.' % (profile, analysis)
    metrics = {
        prefix + 'phase_margin_deg': 180.0 + phase_at_unity,
        prefix + 'unity_loop_frequency_hz': float(unity[0]),
        prefix + 'low_frequency_loop_gain_db': float(magnitude_db[0]),
    }
    phase_crossings = _crossings(xs, phase_deg, -180.0)
    if require_gain_margin:
        phase_crossing = _select_crossing(phase_crossings, crossing_policy, 'phase')
        metrics[prefix + 'gain_margin_db'] = -_interpolate(magnitude_db, phase_crossing)
    elif phase_crossings:
        phase_crossing = _select_crossing(phase_crossings, crossing_policy, 'phase')
        metrics[prefix + 'gain_margin_db'] = -_interpolate(magnitude_db, phase_crossing)
    if any(not math.isfinite(value) for value in metrics.values()):
        raise StabilityError('stability metrics must be finite')
    return metrics

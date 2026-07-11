"""Stable metric extraction for analog optimization results."""

import math
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Optional, Tuple


_MOS_FIELDS = (
    "id", "gm", "gds", "vth", "vgs", "vds", "vdsat", "vbs", "cgg", "cgs", "cgd"
)


def _finite_real(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _finite_curve(x_values: Sequence[Any], y_values: Sequence[Any]) -> Optional[Tuple[list, list]]:
    try:
        if len(x_values) < 2 or len(x_values) != len(y_values):
            return None
    except TypeError:
        return None
    xs = []
    ys = []
    for x_value, y_value in zip(x_values, y_values):
        x = _finite_real(x_value)
        y = _finite_real(y_value)
        if x is None or y is None:
            return None
        xs.append(x)
        ys.append(y)
    if any(right <= left for left, right in zip(xs, xs[1:])):
        return None
    return xs, ys


def extract_mos_op_metrics(instance: str, operating_point: Mapping[str, Any]) -> Dict[str, Any]:
    """Extract available MOS operating-point fields and well-defined derivatives."""
    prefix = f"op.{instance}."
    result: Dict[str, Any] = {}
    values: Dict[str, float] = {}
    for field in _MOS_FIELDS:
        value = _finite_real(operating_point.get(field))
        if value is not None:
            values[field] = value
            result[prefix + field] = value
    region = operating_point.get("region")
    if isinstance(region, str):
        result[prefix + "region"] = region

    gm = values.get("gm")
    drain_current = values.get("id")
    output_conductance = values.get("gds")
    if gm is not None and drain_current not in (None, 0.0):
        derived = abs(gm / drain_current)
        if math.isfinite(derived):
            result[prefix + "gm_over_id"] = derived
    if gm is not None and output_conductance not in (None, 0.0):
        derived = abs(gm / output_conductance)
        if math.isfinite(derived):
            result[prefix + "intrinsic_gain"] = derived
    if "vds" in values and "vdsat" in values:
        result[prefix + "saturation_margin"] = abs(values["vds"]) - abs(values["vdsat"])
    return result


def _complex_curve(frequencies: Sequence[Any], response: Sequence[Any]) -> Optional[Tuple[list, list]]:
    try:
        if len(frequencies) < 2 or len(frequencies) != len(response):
            return None
    except TypeError:
        return None
    xs = []
    values = []
    for frequency, raw_value in zip(frequencies, response):
        x = _finite_real(frequency)
        try:
            value = complex(raw_value)
        except (TypeError, ValueError, OverflowError):
            return None
        if x is None or x <= 0 or not (math.isfinite(value.real) and math.isfinite(value.imag)):
            return None
        magnitude = abs(value)
        if magnitude <= 0 or not math.isfinite(magnitude):
            return None
        xs.append(x)
        values.append(value)
    if any(right <= left for left, right in zip(xs, xs[1:])):
        return None
    return xs, values


def _downward_crossing(frequencies, values, target, start=1, last=False):
    crossings = []
    for index in range(max(1, start), len(values)):
        left = values[index - 1]
        right = values[index]
        if left > target and right <= target:
            fraction = (target - left) / (right - left)
            log_frequency = math.log10(frequencies[index - 1]) + fraction * (
                math.log10(frequencies[index]) - math.log10(frequencies[index - 1])
            )
            crossings.append(10.0 ** log_frequency)
            if not last:
                break
    return crossings[-1] if crossings else None


def extract_ac_metrics(name: str, frequencies: Sequence[Any], response: Sequence[Any]) -> Dict[str, float]:
    """Extract magnitude metrics from a complex AC response."""
    curve = _complex_curve(frequencies, response)
    if curve is None:
        return {}
    xs, values = curve
    gain_db = [20.0 * math.log10(abs(value)) for value in values]
    prefix = f"ac.{name}."
    result = {prefix + "gain_dc_db": gain_db[0], prefix + "gain_peak_db": max(gain_db)}
    threshold = gain_db[0] - 3.0
    if gain_db[0] > threshold:
        bandwidth = _downward_crossing(xs, gain_db, threshold)
        if bandwidth is not None:
            result[prefix + "bandwidth_3db_hz"] = bandwidth
    peak_index = max(range(len(gain_db)), key=gain_db.__getitem__)
    unity_gain = _downward_crossing(xs, gain_db, 0.0, start=peak_index + 1, last=True)
    if unity_gain is not None:
        result[prefix + "unity_gain_hz"] = unity_gain
    return result


def extract_noise_metrics(name: str, frequencies: Sequence[Any], density: Sequence[Any]) -> Dict[str, float]:
    """Extract output noise density and integrated RMS noise."""
    curve = _finite_curve(frequencies, density)
    if curve is None:
        return {}
    xs, values = curve
    if any(frequency <= 0 for frequency in xs) or any(value < 0 for value in values):
        return {}
    result = {f"noise.{name}.output_density_v_per_sqrt_hz": values[0]}
    integral = 0.0
    for index in range(1, len(xs)):
        try:
            area = 0.5 * (values[index - 1] ** 2 + values[index] ** 2) * (xs[index] - xs[index - 1])
            integral += area
        except OverflowError:
            return result
        if not math.isfinite(integral):
            return result
    rms = math.sqrt(integral)
    if math.isfinite(rms):
        result[f"noise.{name}.integrated_output_vrms"] = rms
    return result


def _settling_entry_time(xs, ys, target, band, outside_index):
    left_time, right_time = xs[outside_index], xs[outside_index + 1]
    left_value, right_value = ys[outside_index], ys[outside_index + 1]
    boundary = target + math.copysign(band, left_value - target)
    if right_value == left_value:
        return right_time
    fraction = (boundary - left_value) / (right_value - left_value)
    return left_time + min(1.0, max(0.0, fraction)) * (right_time - left_time)


def extract_tran_metrics(
    name: str, signal: str, times: Sequence[Any], values: Sequence[Any], *, target: Any,
    settling_tolerance: float = 0.02,
) -> Dict[str, float]:
    """Extract step-envelope excursions, settling duration, and slew rates."""
    curve = _finite_curve(times, values)
    target_value = _finite_real(target)
    tolerance = _finite_real(settling_tolerance)
    if curve is None or target_value is None or tolerance is None or tolerance < 0:
        return {}
    xs, ys = curve
    prefix = f"tran.{name}.{signal}."
    result: Dict[str, float] = {}
    initial = ys[0]
    step = target_value - initial
    if step != 0.0:
        scale = abs(step)
        if step > 0:
            overshoot = max(0.0, max(ys) - target_value) / scale
            undershoot = max(0.0, initial - min(ys)) / scale
        else:
            overshoot = max(0.0, target_value - min(ys)) / scale
            undershoot = max(0.0, max(ys) - initial) / scale
        result[prefix + "overshoot"] = overshoot
        result[prefix + "undershoot"] = undershoot

    band = tolerance * abs(target_value)
    outside = [index for index, value in enumerate(ys) if abs(value - target_value) > band]
    if not outside:
        result[prefix + "settling_time_s"] = 0.0
    elif outside[-1] < len(xs) - 1:
        entry = _settling_entry_time(xs, ys, target_value, band, outside[-1])
        result[prefix + "settling_time_s"] = entry - xs[0]

    slopes = [(ys[index] - ys[index - 1]) / (xs[index] - xs[index - 1]) for index in range(1, len(xs))]
    rise = max(slopes)
    fall = min(slopes)
    if rise > 0:
        result[prefix + "slew_rise_v_per_s"] = rise
    if fall < 0:
        result[prefix + "slew_fall_v_per_s"] = abs(fall)
    return result


def merge_metrics(*metric_maps: Mapping[str, float]) -> Dict[str, float]:
    """Merge metric maps deterministically, with later maps taking precedence."""
    result: Dict[str, float] = {}
    for metric_map in metric_maps:
        result.update(metric_map)
    return result

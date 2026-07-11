"""Stable metric extraction for analog optimization results."""

import cmath
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


def extract_mos_op_metrics(instance: str, operating_point: Mapping[str, Any]) -> Dict[str, float]:
    """Extract finite MOS operating-point fields and well-defined derivatives."""
    prefix = f"op.{instance}."
    result: Dict[str, float] = {}
    values: Dict[str, float] = {}
    for field in _MOS_FIELDS:
        value = _finite_real(operating_point.get(field))
        if value is not None:
            values[field] = value
            result[prefix + field] = value

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
        derived = abs(values["vds"]) - abs(values["vdsat"])
        if math.isfinite(derived):
            result[prefix + "saturation_margin"] = derived
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


def _log_frequency_crossing(frequencies: Sequence[float], values: Sequence[float], target: float) -> Optional[float]:
    for index in range(1, len(values)):
        left_value = values[index - 1]
        right_value = values[index]
        if left_value == target:
            return frequencies[index - 1]
        if (left_value - target) * (right_value - target) <= 0 and left_value != right_value:
            fraction = (target - left_value) / (right_value - left_value)
            log_frequency = math.log10(frequencies[index - 1]) + fraction * (
                math.log10(frequencies[index]) - math.log10(frequencies[index - 1])
            )
            crossing = 10.0 ** log_frequency
            return crossing if math.isfinite(crossing) else None
    if values[-1] == target:
        return frequencies[-1]
    return None


def extract_ac_metrics(name: str, frequencies: Sequence[Any], response: Sequence[Any]) -> Dict[str, float]:
    """Extract magnitude metrics from a complex AC response."""
    curve = _complex_curve(frequencies, response)
    if curve is None:
        return {}
    xs, values = curve
    gain_db = [20.0 * math.log10(abs(value)) for value in values]
    prefix = f"ac.{name}."
    result = {
        prefix + "gain_dc_db": gain_db[0],
        prefix + "gain_peak_db": max(gain_db),
    }
    bandwidth = _log_frequency_crossing(xs, gain_db, gain_db[0] - 3.0)
    if bandwidth is not None:
        result[prefix + "bandwidth_3db_hz"] = bandwidth
    unity_gain = _log_frequency_crossing(xs, gain_db, 0.0)
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
    integral = 0.0
    for index in range(1, len(xs)):
        left_squared = values[index - 1] * values[index - 1]
        right_squared = values[index] * values[index]
        area = 0.5 * (left_squared + right_squared) * (xs[index] - xs[index - 1])
        integral += area
        if not math.isfinite(integral):
            return {}
    return {
        f"noise.{name}.output_density_v_per_sqrt_hz": values[0],
        f"noise.{name}.integrated_output_vrms": math.sqrt(integral),
    }


def extract_tran_metrics(
    name: str,
    signal: str,
    times: Sequence[Any],
    values: Sequence[Any],
    *,
    target: Any,
    settling_tolerance: float = 0.02,
) -> Dict[str, float]:
    """Extract target-relative transient excursions, settling, and slew rates."""
    curve = _finite_curve(times, values)
    target_value = _finite_real(target)
    tolerance = _finite_real(settling_tolerance)
    if curve is None or target_value is None or tolerance is None or tolerance < 0:
        return {}
    xs, ys = curve
    prefix = f"tran.{name}.{signal}."
    result: Dict[str, float] = {}

    if target_value != 0.0:
        scale = abs(target_value)
        if target_value > 0:
            overshoot = (max(ys) - target_value) / scale
            undershoot = (target_value - min(ys)) / scale
        else:
            overshoot = (target_value - min(ys)) / scale
            undershoot = (max(ys) - target_value) / scale
        result[prefix + "overshoot"] = max(0.0, overshoot)
        result[prefix + "undershoot"] = max(0.0, undershoot)

    band = tolerance * abs(target_value)
    outside = [index for index, value in enumerate(ys) if abs(value - target_value) > band]
    if not outside:
        result[prefix + "settling_time_s"] = xs[0]
    elif outside[-1] < len(xs) - 1:
        result[prefix + "settling_time_s"] = xs[outside[-1] + 1]

    slopes = [(ys[index] - ys[index - 1]) / (xs[index] - xs[index - 1]) for index in range(1, len(xs))]
    rise = max(slopes)
    fall = min(slopes)
    if rise > 0:
        result[prefix + "slew_rise_v_per_s"] = rise
    if fall < 0:
        result[prefix + "slew_fall_v_per_s"] = fall
    return result


def merge_metrics(*metric_maps: Mapping[str, float]) -> Dict[str, float]:
    """Merge metric maps deterministically, with later maps taking precedence."""
    result: Dict[str, float] = {}
    for metric_map in metric_maps:
        result.update(metric_map)
    return result

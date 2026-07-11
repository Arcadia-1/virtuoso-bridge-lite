import math

import pytest

from analog_opt.metrics import (
    extract_ac_metrics,
    extract_mos_op_metrics,
    extract_noise_metrics,
    extract_tran_metrics,
    merge_metrics,
)


def test_mos_metrics_include_available_raw_and_derived_values():
    metrics = extract_mos_op_metrics(
        "M1", {"id": 10e-6, "gm": 200e-6, "gds": 2e-6, "vds": -0.8, "vdsat": -0.2}
    )
    assert metrics["op.M1.id"] == pytest.approx(10e-6)
    assert metrics["op.M1.gm_over_id"] == pytest.approx(20)
    assert metrics["op.M1.intrinsic_gain"] == pytest.approx(100)
    assert metrics["op.M1.saturation_margin"] == pytest.approx(0.6)


def test_mos_omits_missing_nonfinite_and_undefined_derivatives():
    metrics = extract_mos_op_metrics(
        "M0", {"id": 0.0, "gm": 1e-3, "gds": 0.0, "vds": math.inf, "region": "sat"}
    )
    assert metrics == {"op.M0.id": 0.0, "op.M0.gm": 1e-3, "op.M0.gds": 0.0}


def test_ac_interpolates_crossings_and_never_emits_phase_margin():
    metrics = extract_ac_metrics(
        "main", [1.0, 10.0, 100.0, 1000.0], [100 + 0j, 100j, 10 + 0j, 0.1 + 0j]
    )
    assert metrics["ac.main.gain_dc_db"] == pytest.approx(40.0)
    assert metrics["ac.main.gain_peak_db"] == pytest.approx(40.0)
    assert 10.0 < metrics["ac.main.bandwidth_3db_hz"] < 100.0
    assert 100.0 < metrics["ac.main.unity_gain_hz"] < 1000.0
    assert "ac.main.phase_margin" not in metrics


@pytest.mark.parametrize(
    ("frequencies", "response"),
    [
        ([], []), ([1.0], []), ([1.0, 1.0], [1 + 0j, 0.5 + 0j]),
        ([10.0, 1.0], [1 + 0j, 0.5 + 0j]),
        ([1.0, math.inf], [1 + 0j, 0.5 + 0j]),
        ([1.0, 10.0], [1 + 0j, complex(math.nan, 0.0)]),
    ],
)
def test_ac_invalid_curves_return_no_metrics(frequencies, response):
    assert extract_ac_metrics("bad", frequencies, response) == {}


def test_ac_omits_crossings_outside_sampled_band():
    metrics = extract_ac_metrics("flat", [1.0, 10.0], [10 + 0j, 10 + 0j])
    assert set(metrics) == {"ac.flat.gain_dc_db", "ac.flat.gain_peak_db"}


def test_noise_integrates_density_squared_with_trapezoids():
    metrics = extract_noise_metrics("onoise", [1.0, 2.0, 4.0], [2.0, 2.0, 4.0])
    assert metrics["noise.onoise.output_density_v_per_sqrt_hz"] == pytest.approx(2.0)
    assert metrics["noise.onoise.integrated_output_vrms"] == pytest.approx(math.sqrt(24.0))


@pytest.mark.parametrize(
    ("frequencies", "density"),
    [
        ([], []), ([1.0], [1.0]), ([1.0], []), ([1.0, 1.0], [1.0, 1.0]),
        ([2.0, 1.0], [1.0, 1.0]), ([1.0, 2.0], [1.0, math.nan]),
    ],
)
def test_noise_invalid_curves_return_no_metrics(frequencies, density):
    assert extract_noise_metrics("bad", frequencies, density) == {}


def test_transient_uses_target_and_last_out_of_band_sample():
    metrics = extract_tran_metrics(
        "step", "VOUT", [0.0, 1e-6, 2e-6, 3e-6, 4e-6],
        [0.0, 1.1, 0.97, 1.03, 1.0], target=1.0, settling_tolerance=0.02,
    )
    assert metrics["tran.step.VOUT.overshoot"] == pytest.approx(0.1)
    assert metrics["tran.step.VOUT.undershoot"] == pytest.approx(1.0)
    assert metrics["tran.step.VOUT.settling_time_s"] == pytest.approx(4e-6)
    assert metrics["tran.step.VOUT.slew_rise_v_per_s"] == pytest.approx(1.1e6)
    assert metrics["tran.step.VOUT.slew_fall_v_per_s"] == pytest.approx(-0.13e6)


def test_transient_negative_target_uses_target_magnitude():
    metrics = extract_tran_metrics("neg", "VOUT", [0.0, 1.0, 2.0], [0.0, -1.2, -1.0], target=-1.0)
    assert metrics["tran.neg.VOUT.overshoot"] == pytest.approx(0.2)
    assert metrics["tran.neg.VOUT.undershoot"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("times", "values", "target"),
    [
        ([], [], 1.0), ([0.0], [], 1.0), ([0.0, 0.0], [0.0, 1.0], 1.0),
        ([1.0, 0.0], [0.0, 1.0], 1.0), ([0.0, 1.0], [0.0, math.inf], 1.0),
        ([0.0, 1.0], [0.0, 1.0], math.nan),
    ],
)
def test_transient_invalid_curves_return_no_metrics(times, values, target):
    assert extract_tran_metrics("bad", "VOUT", times, values, target=target) == {}


def test_transient_zero_target_omits_normalized_excursions():
    metrics = extract_tran_metrics("zero", "VOUT", [0.0, 1.0], [0.0, 1.0], target=0.0)
    assert "tran.zero.VOUT.overshoot" not in metrics
    assert "tran.zero.VOUT.undershoot" not in metrics


def test_merge_metrics_uses_later_values():
    assert merge_metrics({"a": 1.0}, {}, {"a": 2.0, "b": 3.0}) == {"a": 2.0, "b": 3.0}

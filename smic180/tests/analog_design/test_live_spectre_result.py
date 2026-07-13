import math

import pytest

from analog_design.simulation.direct_spectre import extract_spectre_result


def test_extract_spectre_result_measures_ac_power_and_open_loop_transient_without_claiming_closed_loop_slew():
    data = {
        "ac_freq": [1.0, 10.0, 100.0],
        "ac_VINP": [0.5 + 0j, 0.5 + 0j, 0.5 + 0j],
        "ac_VINN": [-0.5 + 0j, -0.5 + 0j, -0.5 + 0j],
        "ac_VOUT": [10.0 + 0j, 1.0 + 0j, 0.1 + 0j],
        "dc_VDD": 3.3,
        "dc_VDD_SRC:p": -2e-6,
        "dc_VOUT": 1.2,
        "time": [0.0, 1e-6, 2e-6],
        "VOUT": [1.0, 1.2, 0.8],
        "dc_X_DUT.M1:region": 2.0,
        "dc_X_DUT.M1:gm": 1e-3,
        "dc_X_DUT.M1:gds": 1e-5,
        "dc_X_DUT.M1:vds": 1.2,
        "dc_X_DUT.M1:vdsat": 0.2,
        "dc_X_DUT.M1:id": 1e-4,
    }

    parsed = extract_spectre_result(data, dut_instance="X_DUT", transient_scope="open_loop_differential_step")

    assert parsed["measurements"]["gain"] == pytest.approx(20.0)
    assert parsed["measurements"]["ugbw"] == pytest.approx(10.0)
    assert parsed["measurements"]["supply_current"] == pytest.approx(2e-6)
    assert parsed["measurements"]["power"] == pytest.approx(6.6e-6)
    assert parsed["measurements"]["output_dc"] == pytest.approx(1.2)
    assert parsed["measurements"]["open_loop_slew_rate"] == pytest.approx(4e5)
    assert "slew_rate" not in parsed["measurements"]
    assert parsed["measurement_scopes"]["open_loop_slew_rate"] == "open_loop_differential_step"
    assert parsed["operating_points"]["M1"]["region"] == 2.0
    assert parsed["sample_counts"] == {"ac": 3, "tran": 3, "op": 1}


def test_extract_spectre_result_rejects_missing_or_nonfinite_primary_waveforms():
    with pytest.raises(ValueError, match="AC"):
        extract_spectre_result({}, dut_instance="X_DUT", transient_scope="open_loop_differential_step")
    with pytest.raises(ValueError, match="finite"):
        extract_spectre_result(
            {
                "ac_freq": [1.0],
                "ac_VINP": [0.5 + 0j],
                "ac_VINN": [-0.5 + 0j],
                "ac_VOUT": [complex(math.nan, 0.0)],
            },
            dut_instance="X_DUT",
            transient_scope="open_loop_differential_step",
        )
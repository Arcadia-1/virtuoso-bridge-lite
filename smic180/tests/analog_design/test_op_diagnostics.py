import pytest

from analog_design.simulation.diagnostics import DiagnosticError, diagnose_mos_operating_points


def test_diagnostics_compute_gm_over_id_and_saturation_margin():
    diagnostics = diagnose_mos_operating_points({"M1": {"region": "saturation", "gm": 200e-6, "gds": 2e-6, "vds": 1.0, "vdsat": 0.2, "id": 20e-6}})
    assert diagnostics["M1"]["gm_over_id"] == pytest.approx(10.0)
    assert diagnostics["M1"]["intrinsic_gain"] == pytest.approx(100.0)
    assert diagnostics["M1"]["saturation_margin"] == pytest.approx(0.8)


def test_diagnostics_reject_missing_nonfinite_or_zero_current():
    with pytest.raises(DiagnosticError):
        diagnose_mos_operating_points({"M1": {"gm": 1.0}})
    with pytest.raises(DiagnosticError):
        diagnose_mos_operating_points({"M1": {"region": "saturation", "gm": 1.0, "gds": 1.0, "vds": 1.0, "vdsat": 0.2, "id": 0.0}})

import math

import pytest

from analog_design.units import UnitError, parse_quantity


def test_parse_quantity_normalizes_supported_si_values():
    assert parse_quantity("3.3V", "voltage") == pytest.approx(3.3)
    assert parse_quantity("10uA", "current") == pytest.approx(10e-6)
    assert parse_quantity("5pF", "capacitance") == pytest.approx(5e-12)
    assert parse_quantity("10MHz", "frequency") == pytest.approx(10e6)
    assert parse_quantity("5V/us", "slew_rate") == pytest.approx(5e6)


def test_parse_quantity_accepts_finite_numeric_si_values():
    assert parse_quantity(1.25, "voltage") == pytest.approx(1.25)


@pytest.mark.parametrize("value", [True, False, math.nan, math.inf, -math.inf, "nanV", "1A"])
def test_parse_quantity_rejects_boolean_nonfinite_and_wrong_dimension(value):
    with pytest.raises(UnitError):
        parse_quantity(value, "voltage")

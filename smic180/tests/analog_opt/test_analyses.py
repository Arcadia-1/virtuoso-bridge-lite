import math

import pytest

from analog_opt.analyses import (
    AnalysisError,
    build_analysis_lines,
    is_curve_analysis,
    required_source_parameters,
)


def test_builds_supported_spectre_analysis_lines():
    analyses = [
        {"name": "op", "type": "dc_op"},
        {"name": "vdd", "type": "dc_sweep", "parameter": "VDD_SWEEP", "source": "VDD", "start": 2.7, "stop": 3.6, "points": 91},
        {"name": "ac_main", "type": "ac", "start": "1Hz", "stop": "1GHz", "points_per_decade": 100},
        {"name": "onoise", "type": "noise", "input_source": "VIN", "output": "VOUT", "start": "1Hz", "stop": "100MHz", "points_per_decade": 50},
        {"name": "step", "type": "tran", "stop": "20us", "max_step": "10ns", "errpreset": "conservative"},
    ]
    assert build_analysis_lines(analyses) == [
        "op dc",
        "vdd dc param=VDD_SWEEP start=2.7 stop=3.6 lin=90",
        "ac_main ac start=1 stop=1000000000 dec=100",
        "onoise (VOUT 0) noise iprobe=VIN start=1 stop=100000000 dec=50",
        "step tran stop=2e-05 maxstep=1e-08 errpreset=conservative",
    ]


def test_noise_node_output_supports_explicit_reference():
    analysis = {
        "name": "inoise",
        "type": "noise",
        "input_source": "VIN_SRC",
        "output": "VOUT_P",
        "output_reference": "VOUT_N",
        "start": "10Hz",
        "stop": "1MHz",
        "points_per_decade": 20,
    }
    assert build_analysis_lines([analysis]) == [
        "inoise (VOUT_P VOUT_N) noise iprobe=VIN_SRC start=10 stop=1000000 dec=20"
    ]


def test_dc_op_is_not_curve_but_real_sweeps_are():
    assert not is_curve_analysis({"type": "dc_op"})
    assert is_curve_analysis({"type": "dc_sweep"})
    assert is_curve_analysis({"type": "ac"})
    assert is_curve_analysis({"type": "noise"})
    assert is_curve_analysis({"type": "tran"})


def test_stb_analysis_renders_probe_and_frequency_sweep():
    analysis = {
        'name': 'loop', 'type': 'stb', 'probe': 'IPRB',
        'start': 1.0, 'stop': 1e9, 'points_per_decade': 50,
    }
    assert build_analysis_lines([analysis]) == [
        'loop stb probe=IPRB start=1 stop=1000000000 dec=50'
    ]
    assert is_curve_analysis(analysis)


def test_stb_analysis_requires_probe():
    with pytest.raises(AnalysisError, match='probe'):
        build_analysis_lines([{
            'name': 'loop', 'type': 'stb', 'start': 1,
            'stop': 1e9, 'points_per_decade': 50,
        }])


def test_required_source_parameters_returns_mapping():
    analyses = [
        {"name": "vdd", "type": "dc_sweep", "source": "VDD", "parameter": "VDD_SWEEP", "start": 0, "stop": 1, "points": 2},
        {"name": "op", "type": "dc_op"},
    ]
    assert required_source_parameters(analyses) == {"VDD": "VDD_SWEEP"}


def test_required_source_parameters_rejects_conflicting_mapping():
    analyses = [
        {"name": "a", "type": "dc_sweep", "source": "VDD", "parameter": "P1"},
        {"name": "b", "type": "dc_sweep", "source": "VDD", "parameter": "P2"},
    ]
    with pytest.raises(AnalysisError, match="conflicting.*VDD"):
        required_source_parameters(analyses)


@pytest.mark.parametrize(
    ("analysis", "message"),
    [
        ({"name": "1bad", "type": "dc_op"}, "name"),
        ({"name": "dup", "type": "bogus"}, "unsupported"),
        ({"name": "bad", "type": "dc_sweep", "start": 0, "stop": 1, "points": 2}, "parameter"),
        ({"name": "bad", "type": "dc_sweep", "parameter": "X", "start": 0, "stop": 1, "points": 2}, "source"),
        ({"name": "bad", "type": "dc_sweep", "parameter": "X", "source": "V", "start": 0, "stop": 1, "points": 1}, "at least 2"),
        ({"name": "bad", "type": "dc_sweep", "parameter": "X", "source": "V", "start": 0, "stop": 1, "points": 2.5}, "integer"),
        ({"name": "bad", "type": "ac", "start": "1Hz", "stop": "1GHz", "points_per_decade": 0}, "positive"),
        ({"name": "bad", "type": "noise", "input_source": "VIN", "start": "1Hz", "stop": "1GHz", "points_per_decade": 10}, "output"),
        ({"name": "bad", "type": "tran", "stop": math.inf}, "finite"),
        ({"name": "bad", "type": "tran", "stop": "1us", "errpreset": "reckless"}, "errpreset"),
    ],
)
def test_rejects_invalid_analysis(analysis, message):
    with pytest.raises(AnalysisError, match=message):
        build_analysis_lines([analysis])


def test_analysis_names_must_be_unique():
    with pytest.raises(AnalysisError, match="unique"):
        build_analysis_lines([{"name": "op", "type": "dc_op"}, {"name": "op", "type": "dc_op"}])


@pytest.mark.parametrize(
    "analysis",
    [
        {"name": "dc", "type": "dc_sweep", "parameter": "X", "source": "V", "start": 1, "stop": 1, "points": 2},
        {"name": "ac", "type": "ac", "start": "1GHz", "stop": "1Hz", "points_per_decade": 10},
        {"name": "tran", "type": "tran", "stop": 0},
    ],
)
def test_ranges_must_increase_or_be_positive(analysis):
    with pytest.raises(AnalysisError):
        build_analysis_lines([analysis])

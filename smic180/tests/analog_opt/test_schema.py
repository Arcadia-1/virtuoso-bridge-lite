import ast
import json
from pathlib import Path
from typing import Optional, get_type_hints

import pytest

from analog_opt.schema import (
    AnalogOptConfig,
    ConfigError,
    DesignConfig,
    StimulusConfig,
    load_config,
)


def minimal_config():
    return {
        "version": 2,
        "design": {
            "library": "tr",
            "cell": "amp",
            "work_cell": "amp_opt_work",
            "result_cell": "amp_opt",
            "testbench_cell": "amp_opt_tb",
        },
        "stimuli": {"VDD": {"kind": "voltage", "value": "3.3V"}},
        "parameters": [],
        "analyses": [{"name": "op", "type": "dc_op"}],
        "metrics": [],
        "specs": [],
        "search": {"algorithm": "random", "max_evals": 5, "seed": 7},
        "pvt": {"corners": ["tt"], "voltages": ["3.3V"], "temperatures_c": [27], "voltage_stimulus": "VDD"},
        "outputs": {},
    }


def write_config(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_loads_v2_config_as_dataclasses(tmp_path):
    config = load_config(write_config(tmp_path, minimal_config()))
    assert isinstance(config, AnalogOptConfig)
    assert isinstance(config.design, DesignConfig)
    assert isinstance(config.stimuli["VDD"], StimulusConfig)
    assert config.version == 2


@pytest.mark.parametrize("version", [1, 2.0, True, False, "2"])
def test_version_must_be_exact_integer_two(tmp_path, version):
    data = minimal_config()
    data["version"] = version
    with pytest.raises(ConfigError, match="version must be 2"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("missing_key", list(minimal_config()))
def test_missing_top_level_required_key_names_key(tmp_path, missing_key):
    data = minimal_config()
    del data[missing_key]
    with pytest.raises(ConfigError, match=missing_key):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("field", ["library", "cell", "work_cell", "result_cell", "testbench_cell"])
@pytest.mark.parametrize("invalid_value", [None, 7, "", "   "])
def test_design_fields_must_be_nonempty_strings(tmp_path, field, invalid_value):
    data = minimal_config()
    data["design"][field] = invalid_value
    with pytest.raises(ConfigError, match=field):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [("work_cell", "amp"), ("result_cell", "amp"), ("result_cell", "amp_opt_work")],
)
def test_source_work_and_result_cells_must_be_distinct(tmp_path, field, duplicate):
    data = minimal_config()
    data["design"][field] = duplicate
    with pytest.raises(ConfigError, match="must be distinct"):
        load_config(write_config(tmp_path, data))


def test_fixed_stimulus_quantities_are_stored_as_si_values(tmp_path):
    data = minimal_config()
    data["stimuli"] = {
        "VDD": {"kind": "voltage", "value": "3300mV"},
        "VIN": {"kind": "voltage", "dc": "1200mV", "ac": 1},
        "IBIAS": {"kind": "current", "value": "10uA"},
    }
    stimuli = load_config(write_config(tmp_path, data)).stimuli
    assert stimuli["VDD"].value == pytest.approx(3.3)
    assert stimuli["VIN"].dc == pytest.approx(1.2)
    assert stimuli["VIN"].ac == pytest.approx(1.0)
    assert stimuli["IBIAS"].value == pytest.approx(10e-6)


@pytest.mark.parametrize("kind", ["resistance", "", None, []])
def test_rejects_unknown_stimulus_kind(tmp_path, kind):
    data = minimal_config()
    data["stimuli"]["VDD"]["kind"] = kind
    with pytest.raises(ConfigError, match="kind"):
        load_config(write_config(tmp_path, data))


def test_rejects_stimulus_quantity_with_wrong_dimension(tmp_path):
    data = minimal_config()
    data["stimuli"]["VDD"]["value"] = "10uA"
    with pytest.raises(ConfigError, match="value"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("field", ["value", "dc", "ac"])
def test_huge_fixed_stimulus_number_raises_config_error(tmp_path, field):
    data = minimal_config()
    data["stimuli"]["VDD"].pop("value", None)
    data["stimuli"]["VDD"][field] = 10**1000
    with pytest.raises(ConfigError, match=field):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("ac", [None, "1", True, float("nan"), float("inf")])
def test_rejects_nonfinite_or_nonnumeric_ac_value(tmp_path, ac):
    data = minimal_config()
    data["stimuli"]["VDD"]["ac"] = ac
    with pytest.raises(ConfigError, match="ac"):
        load_config(write_config(tmp_path, data))


def test_fixed_stimulus_defaults_to_not_optimizable_without_bounds(tmp_path):
    config = load_config(write_config(tmp_path, minimal_config()))
    assert config.stimuli["VDD"].optimizable is False
    assert config.stimuli["VDD"].lower is None
    assert config.stimuli["VDD"].upper is None


@pytest.mark.parametrize("missing_bound", ["lower", "upper"])
def test_optimizable_stimulus_requires_both_bounds(tmp_path, missing_bound):
    data = minimal_config()
    data["stimuli"]["VDD"].update({"optimizable": True, "lower": "2.7V", "upper": "3.6V"})
    del data["stimuli"]["VDD"][missing_bound]
    with pytest.raises(ConfigError, match=missing_bound):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("field", ["lower", "upper"])
def test_huge_optimizable_bound_raises_config_error(tmp_path, field):
    data = minimal_config()
    bounds = {"lower": "2.7V", "upper": "3.6V"}
    bounds[field] = 10**1000
    data["stimuli"]["VDD"].update({"optimizable": True, **bounds})
    with pytest.raises(ConfigError, match=field):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize(
    ("lower", "upper"),
    [
        (None, "3.6V"),
        ("2.7V", None),
        ("invalid", "3.6V"),
        ("2.7V", "invalid"),
        (float("nan"), 3.6),
        (2.7, float("inf")),
        ("3.3V", "3.3V"),
        ("3.6V", "2.7V"),
    ],
)
def test_optimizable_stimulus_requires_valid_increasing_bounds(tmp_path, lower, upper):
    data = minimal_config()
    data["stimuli"]["VDD"].update(
        {"optimizable": True, "lower": lower, "upper": upper}
    )
    with pytest.raises(ConfigError, match="bounds|lower|upper"):
        load_config(write_config(tmp_path, data))


def test_optimizable_stimulus_accepts_numeric_bounds(tmp_path):
    data = minimal_config()
    data["stimuli"]["VDD"].update(
        {"optimizable": True, "lower": 2.7, "upper": 3.6}
    )
    stimulus = load_config(write_config(tmp_path, data)).stimuli["VDD"]
    assert stimulus.lower == 2.7
    assert stimulus.upper == 3.6


def test_optimizable_stimulus_stores_bounds_as_si_floats(tmp_path):
    data = minimal_config()
    data["stimuli"]["VDD"].update({"optimizable": True, "lower": "2700mV", "upper": "3.6V"})
    stimulus = load_config(write_config(tmp_path, data)).stimuli["VDD"]
    assert stimulus.optimizable is True
    assert stimulus.lower == pytest.approx(2.7)
    assert stimulus.upper == pytest.approx(3.6)
    hints = get_type_hints(StimulusConfig)
    assert hints["lower"] == Optional[float]
    assert hints["upper"] == Optional[float]


@pytest.mark.parametrize("name", [None, 3, [], "", "   "])
def test_parameter_name_must_be_nonempty_string(tmp_path, name):
    data = minimal_config()
    data["parameters"] = [{"name": name, "target": "bias"}]
    with pytest.raises(ConfigError, match="parameter.*name"):
        load_config(write_config(tmp_path, data))


def test_parameter_names_must_be_unique(tmp_path):
    data = minimal_config()
    parameter = {"name": "M1_W", "target": "virtuoso_cdf"}
    data["parameters"] = [parameter, dict(parameter)]
    with pytest.raises(ConfigError, match="parameter name.*unique"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("target", ["virtuoso_cdf", "bias", "spectre_variable"])
def test_accepts_supported_parameter_targets(tmp_path, target):
    data = minimal_config()
    if target == "bias":
        data["stimuli"]["VDD"].update({"optimizable": True, "lower": "2.7V", "upper": "3.6V"})
        data["parameters"] = [{"name": "P", "target": target, "stimulus": "VDD"}]
    else:
        data["parameters"] = [{"name": "P", "target": target}]
    assert load_config(write_config(tmp_path, data)).parameters[0]["target"] == target


def test_rejects_unsupported_parameter_target(tmp_path):
    data = minimal_config()
    data["parameters"] = [{"name": "P", "target": "netlist_text"}]
    with pytest.raises(ConfigError, match="target"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("name", [None, 3, [], "", "   "])
def test_analysis_name_must_be_nonempty_string(tmp_path, name):
    data = minimal_config()
    data["analyses"] = [{"name": name, "type": "dc_op"}]
    with pytest.raises(ConfigError, match="analysis.*name"):
        load_config(write_config(tmp_path, data))


def test_analysis_names_must_be_unique(tmp_path):
    data = minimal_config()
    data["analyses"].append({"name": "op", "type": "ac"})
    with pytest.raises(ConfigError, match="analysis name.*unique"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("analysis_type", ["dc_op", "dc_sweep", "ac", "noise", "tran"])
def test_accepts_supported_analysis_types(tmp_path, analysis_type):
    data = minimal_config()
    data["analyses"] = [{"name": "analysis", "type": analysis_type}]
    assert load_config(write_config(tmp_path, data)).analyses[0]["type"] == analysis_type


def test_rejects_unsupported_analysis_type(tmp_path):
    data = minimal_config()
    data["analyses"] = [{"name": "bad", "type": "monte_carlo"}]
    with pytest.raises(ConfigError, match="analysis type"):
        load_config(write_config(tmp_path, data))


def test_schema_module_uses_python_39_compatible_annotations():
    schema_path = Path(__file__).resolve().parents[2] / "skills" / "smic180-simulator" / "analog_opt" / "schema.py"
    module = ast.parse(schema_path.read_text(encoding="utf-8-sig"))
    annotations = [node.annotation for node in ast.walk(module) if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation is not None]
    assert not any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
        for annotation in annotations
        for node in ast.walk(annotation)
    )

def test_schema_preserves_sync_property(tmp_path):
    config = minimal_config()
    config["parameters"] = [{"name": "M1_W", "target": "virtuoso_cdf", "sync_property": "fw"}]
    path = write_config(tmp_path, config)
    loaded = load_config(path)
    assert loaded.parameters[0]["sync_property"] == "fw"

def test_stimulus_source_instance_and_pvt_voltage_stimulus_are_explicit(tmp_path):
 data=minimal_config()
 data['stimuli']['VDD']['source_instance']='SUPPLY_MAIN'
 data['pvt']={'corners':['TT'],'voltages':[3.0,3.3],'temperatures':[25],'voltage_stimulus':'VDD'}
 path=write_config(tmp_path,data)
 config=load_config(path)
 assert config.stimuli['VDD'].source_instance=='SUPPLY_MAIN'
 assert config.pvt['voltage_stimulus']=='VDD'

def test_pvt_voltage_stimulus_must_reference_voltage_stimulus(tmp_path):
 data=minimal_config(); data['pvt']={'corners':['TT'],'voltages':[3.3],'temperatures':[25],'voltage_stimulus':'MISSING'}
 with pytest.raises(ConfigError,match='voltage_stimulus'): load_config(write_config(tmp_path,data))

@pytest.mark.parametrize('value',['1BAD','SRC-VDD','SRC VDD',''])
def test_source_instance_must_be_safe_identifier(tmp_path,value):
 data=minimal_config(); data['stimuli']['VDD']['source_instance']=value
 with pytest.raises(ConfigError,match='source_instance'): load_config(write_config(tmp_path,data))

def test_source_instances_must_be_unique(tmp_path):
 data=minimal_config(); data['stimuli']['VIN']={'kind':'voltage','value':'1V','source_instance':'SRC_VDD'}
 with pytest.raises(ConfigError,match='source_instance'): load_config(write_config(tmp_path,data))

def test_bias_parameter_requires_existing_optimizable_stimulus(tmp_path):
 data=minimal_config(); data['parameters']=[{'name':'BIAS','target':'bias','stimulus':'MISSING','lower':0.5,'upper':1.5}]
 with pytest.raises(ConfigError,match='bias'): load_config(write_config(tmp_path,data))
 data['parameters'][0]['stimulus']='VDD'
 with pytest.raises(ConfigError,match='optimizable'): load_config(write_config(tmp_path,data))

def test_pvt_voltage_grid_requires_voltage_stimulus(tmp_path):
 data=minimal_config(); data['pvt']={'corners':['tt'],'voltages':[3.0,3.3],'temperatures_c':[25]}
 with pytest.raises(ConfigError,match='voltage_stimulus'): load_config(write_config(tmp_path,data))

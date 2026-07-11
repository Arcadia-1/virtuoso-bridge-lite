import ast
import json
from pathlib import Path

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
        "pvt": {"corners": ["tt"], "voltages": ["3.3V"], "temperatures_c": [27]},
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


def test_rejects_old_version(tmp_path):
    data = minimal_config()
    data["version"] = 1
    with pytest.raises(ConfigError, match="version must be 2"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("missing_key", list(minimal_config()))
def test_missing_top_level_required_key_names_key(tmp_path, missing_key):
    data = minimal_config()
    del data[missing_key]
    with pytest.raises(ConfigError, match=missing_key):
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


def test_optimizable_stimulus_accepts_both_bounds(tmp_path):
    data = minimal_config()
    data["stimuli"]["VDD"].update({"optimizable": True, "lower": "2.7V", "upper": "3.6V"})
    stimulus = load_config(write_config(tmp_path, data)).stimuli["VDD"]
    assert stimulus.optimizable is True
    assert stimulus.lower == "2.7V"
    assert stimulus.upper == "3.6V"


def test_parameter_names_must_be_unique(tmp_path):
    data = minimal_config()
    parameter = {"name": "M1_W", "target": "virtuoso_cdf"}
    data["parameters"] = [parameter, dict(parameter)]
    with pytest.raises(ConfigError, match="parameter name.*unique"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("target", ["virtuoso_cdf", "bias", "spectre_variable"])
def test_accepts_supported_parameter_targets(tmp_path, target):
    data = minimal_config()
    data["parameters"] = [{"name": "P", "target": target}]
    assert load_config(write_config(tmp_path, data)).parameters[0]["target"] == target


def test_rejects_unsupported_parameter_target(tmp_path):
    data = minimal_config()
    data["parameters"] = [{"name": "P", "target": "netlist_text"}]
    with pytest.raises(ConfigError, match="target"):
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

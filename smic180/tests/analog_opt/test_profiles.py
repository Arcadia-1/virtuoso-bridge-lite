import json

import pytest

from analog_opt.profiles import VerificationProfileConfig
from analog_opt.schema import ConfigError, canonical_resolved_payload, load_config
from test_schema import minimal_config, write_config


def explicit_profile(profile_id='open_loop', testbench_cell='amp_open_loop_tb'):
    return {
        'id': profile_id,
        'role': 'open_loop_small_signal',
        'testbench_cell': testbench_cell,
        'dut_instance': 'DUT',
        'stimuli': {'VDD': {'kind': 'voltage', 'value': '3.3V'}},
        'analyses': [{'name': 'op', 'type': 'dc_op'}],
        'metrics': [{'name': 'op_metrics', 'analysis': 'op'}],
        'specs': [{'metric': 'op_metrics', 'op': '>=', 'value': 0.0}],
        'pvt_policy': 'full',
        'timeout_s': 900,
    }


def test_legacy_config_normalizes_to_default_profile(tmp_path):
    config = load_config(write_config(tmp_path, minimal_config()))
    assert len(config.verification_profiles) == 1
    profile = config.verification_profiles[0]
    assert isinstance(profile, VerificationProfileConfig)
    assert profile.id == 'default'
    assert profile.role == 'legacy'
    assert profile.testbench_cell == config.design.testbench_cell
    assert profile.dut_instance == config.design.dut_instance
    assert profile.stimuli['VDD']['value'] == pytest.approx(3.3)


def test_explicit_profiles_are_parsed_and_round_trip(tmp_path):
    data = minimal_config()
    data['verification_profiles'] = [explicit_profile()]
    config = load_config(write_config(tmp_path, data))
    profile = config.verification_profiles[0]
    assert profile.id == 'open_loop'
    assert profile.timeout_s == 900
    assert profile.stimuli['VDD']['value'] == pytest.approx(3.3)
    resolved = tmp_path / 'resolved.json'
    resolved.write_text(json.dumps(canonical_resolved_payload(config)), encoding='utf-8')
    assert load_config(resolved) == config


def test_profile_ids_and_testbench_cells_must_be_unique(tmp_path):
    data = minimal_config()
    data['verification_profiles'] = [explicit_profile(), explicit_profile()]
    with pytest.raises(ConfigError, match='profile id must be unique'):
        load_config(write_config(tmp_path, data))
    data['verification_profiles'] = [
        explicit_profile(), explicit_profile('slew', 'amp_open_loop_tb')
    ]
    with pytest.raises(ConfigError, match='testbench cell must be unique'):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize('policy', ['bad', '', None])
def test_profile_pvt_policy_is_strict(tmp_path, policy):
    data = minimal_config()
    profile = explicit_profile()
    profile['pvt_policy'] = policy
    data['verification_profiles'] = [profile]
    with pytest.raises(ConfigError, match='pvt_policy'):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize('timeout', [0, -1, 1.5, True])
def test_profile_timeout_must_be_positive_integer(tmp_path, timeout):
    data = minimal_config()
    profile = explicit_profile()
    profile['timeout_s'] = timeout
    data['verification_profiles'] = [profile]
    with pytest.raises(ConfigError, match='timeout_s'):
        load_config(write_config(tmp_path, data))


def test_profile_spec_must_reference_declared_metric(tmp_path):
    data = minimal_config()
    profile = explicit_profile()
    profile['specs'][0]['metric'] = 'missing'
    data['verification_profiles'] = [profile]
    with pytest.raises(ConfigError, match='profile spec metric must exist'):
        load_config(write_config(tmp_path, data))

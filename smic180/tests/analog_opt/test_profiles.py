import json

import pytest

from analog_opt.profiles import MultiProfileBackend, ProfileRuntime, VerificationProfileConfig
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


def test_multi_profile_backend_applies_candidate_once_and_aggregates(tmp_path):
    applied = []
    calls = []

    def evaluate(profile_id, metric, objective):
        def call(candidate, directory, conditions):
            calls.append((profile_id, directory.relative_to(tmp_path).as_posix(), dict(conditions)))
            return {
                'objective': objective, 'success': True,
                'metrics': {metric: objective},
                'specs': {metric: {'passed': True, 'violation': 0.0}},
                'metadata': {'profile_id': profile_id},
            }
        return call

    backend = MultiProfileBackend(
        lambda candidate: applied.append(dict(candidate)),
        [
            ProfileRuntime('open_loop', evaluate('open_loop', 'ac.gain', 1.0)),
            ProfileRuntime('stability', evaluate('stability', 'stb.pm', 2.0)),
            ProfileRuntime('closed_loop_slew', evaluate('closed_loop_slew', 'tran.slew', 3.0)),
        ],
    )
    result = backend({'W': 10e-6}, tmp_path, {'corner': 'TT'})
    assert applied == [{'W': 10e-6}]
    assert [item[0] for item in calls] == ['open_loop', 'stability', 'closed_loop_slew']
    assert result['objective'] == pytest.approx(6.0)
    assert set(result['metrics']) == {'ac.gain', 'stb.pm', 'tran.slew'}
    assert list(result['metadata']['profiles']) == ['open_loop', 'stability', 'closed_loop_slew']
    assert all('/profiles/' in '/' + item[1] + '/' for item in calls)


def test_required_profile_failure_returns_finite_penalty(tmp_path):
    def fail(candidate, directory, conditions):
        raise RuntimeError('missing loop-gain trace')

    backend = MultiProfileBackend(
        lambda candidate: None,
        [ProfileRuntime('stability', fail)],
        failure_penalty=1e12,
    )
    result = backend({'W': 10e-6}, tmp_path)
    assert result['success'] is False
    assert result['objective'] == pytest.approx(1e12)
    assert result['failure']['category'] == 'profile'
    assert result['metadata']['failure_detail']['profile_id'] == 'stability'
    assert result['metadata']['failure_detail']['stage'] == 'evaluation'


def test_multi_profile_backend_resumes_after_interruption(tmp_path):
    first_calls = []
    second_calls = []

    def first(candidate, directory, conditions):
        first_calls.append(directory)
        return {'objective': 1.0, 'success': True, 'metrics': {'a': 1.0}, 'specs': {}, 'metadata': {}}

    def interrupt(candidate, directory, conditions):
        raise KeyboardInterrupt()

    interrupted = MultiProfileBackend(
        lambda candidate: None,
        [ProfileRuntime('first', first), ProfileRuntime('second', interrupt)],
    )
    with pytest.raises(KeyboardInterrupt):
        interrupted({'W': 10e-6}, tmp_path)

    def second(candidate, directory, conditions):
        second_calls.append(directory)
        return {'objective': 2.0, 'success': True, 'metrics': {'b': 2.0}, 'specs': {}, 'metadata': {}}

    resumed = MultiProfileBackend(
        lambda candidate: None,
        [
            ProfileRuntime('first', lambda *args: pytest.fail('completed profile reran')),
            ProfileRuntime('second', second),
        ],
    )
    result = resumed({'W': 10e-6}, tmp_path)
    assert result['success'] is True
    assert len(first_calls) == 1
    assert len(second_calls) == 1
    assert result['metadata']['resumed_profiles'] == ['first']

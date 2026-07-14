import pytest

from analog_opt.profile_testbenches import ProfileTestbenchError, confirm_profile_netlist
from analog_opt.profiles import VerificationProfileConfig


def profile(profile_id, role, analysis):
    return VerificationProfileConfig(
        id=profile_id, role=role, testbench_cell=profile_id + '_tb',
        dut_instance='DUT', stimuli={}, analyses=(analysis,), metrics=(), specs=(),
    )


STB_NETLIST = '''
simulator lang=spectre
DUT (VINP VINN VOUT VDD VSS) amp_work
IPRB (VOUT VINN) iprobe
SUPPLY (VDD 0) vsource type=dc dc=3.3
loop stb probe=IPRB start=1 stop=1e9 dec=50
'''


STB_EXPECTATION = {
    'dut_cell': 'amp_work',
    'instances': {
        'IPRB': {'model': 'iprobe', 'nodes': ['VOUT', 'VINN'], 'parameters': {}},
        'SUPPLY': {'model': 'vsource', 'nodes': ['VDD', '0'], 'parameters': {'dc': 3.3}},
    },
    'analyses': {
        'loop': {'type': 'stb', 'parameters': {'probe': 'IPRB', 'start': 1.0, 'stop': 1e9, 'dec': 50}},
    },
    'probe': {'instance': 'IPRB', 'plus': 'VOUT', 'minus': 'VINN'},
}


def test_stability_confirmation_requires_oriented_iprobe():
    confirmation = confirm_profile_netlist(
        profile('stability', 'unity_gain_stability', {'name': 'loop', 'type': 'stb'}),
        STB_NETLIST,
        STB_EXPECTATION,
    )
    assert confirmation.dut_cell == 'amp_work'
    assert confirmation.probe == {'instance': 'IPRB', 'plus': 'VOUT', 'minus': 'VINN'}
    assert confirmation.analyses['loop']['parameters']['probe'] == 'IPRB'
    assert len(confirmation.netlist_sha256) == 64


SLEW_NETLIST = '''
simulator lang=spectre
DUT (VINP VOUT VOUT VDD VSS) amp_work
VIN_STEP (VINP 0) vsource type=pulse val0=.7 val1=1.1 delay=1u rise=10n fall=10n width=5u period=10u
CLOAD (VOUT 0) capacitor c=2p
step tran stop=20u maxstep=10n
'''


SLEW_EXPECTATION = {
    'dut_cell': 'amp_work',
    'instances': {
        'VIN_STEP': {
            'model': 'vsource', 'nodes': ['VINP', '0'],
            'parameters': {'type': 'pulse', 'val0': 0.7, 'val1': 1.1, 'period': 10e-6},
        },
        'CLOAD': {'model': 'capacitor', 'nodes': ['VOUT', '0'], 'parameters': {'c': 2e-12}},
    },
    'analyses': {
        'step': {'type': 'tran', 'parameters': {'stop': 20e-6, 'maxstep': 10e-9}},
    },
    'pulse': {'instance': 'VIN_STEP', 'low': 0.7, 'high': 1.1},
}


def test_slew_confirmation_proves_pulse_load_and_feedback_connection():
    confirmation = confirm_profile_netlist(
        profile('closed_loop_slew', 'unity_gain_slew', {'name': 'step', 'type': 'tran'}),
        SLEW_NETLIST,
        SLEW_EXPECTATION,
    )
    assert confirmation.instances['VIN_STEP']['parameters']['val1'] == pytest.approx(1.1)
    assert confirmation.instances['CLOAD']['parameters']['c'] == pytest.approx(2e-12)
    assert confirmation.dut_nodes == ('VINP', 'VOUT', 'VOUT', 'VDD', 'VSS')


@pytest.mark.parametrize('netlist,expectation,match', [
    (STB_NETLIST.replace('amp_work', 'stale_work'), STB_EXPECTATION, 'DUT cell'),
    (STB_NETLIST.replace('IPRB (VOUT VINN)', 'IPRB (VINN VOUT)'), STB_EXPECTATION, 'IPRB nodes'),
    (SLEW_NETLIST.replace('val1=1.1', 'val1=.9'), SLEW_EXPECTATION, 'VIN_STEP parameter val1'),
    (SLEW_NETLIST.replace('CLOAD (VOUT 0)', 'CLOAD (VINP 0)'), SLEW_EXPECTATION, 'CLOAD nodes'),
])
def test_profile_confirmation_rejects_structural_mismatch(netlist, expectation, match):
    selected = profile(
        'stability' if expectation is STB_EXPECTATION else 'closed_loop_slew',
        'unity_gain_stability' if expectation is STB_EXPECTATION else 'unity_gain_slew',
        {'name': 'analysis', 'type': 'stb' if expectation is STB_EXPECTATION else 'tran'},
    )
    with pytest.raises(ProfileTestbenchError, match=match):
        confirm_profile_netlist(selected, netlist, expectation)


def test_profile_confirmation_handles_line_continuation_and_reordering():
    text = '''
simulator lang=spectre
loop stb probe=IPRB start=1 stop=1e9 dec=50
SUPPLY (VDD 0) vsource type=dc \\
  dc=3.3
IPRB (VOUT VINN) iprobe
DUT (VINP VINN VOUT VDD VSS) amp_work
'''
    confirmation = confirm_profile_netlist(
        profile('stability', 'unity_gain_stability', {'name': 'loop', 'type': 'stb'}),
        text,
        STB_EXPECTATION,
    )
    assert confirmation.instances['SUPPLY']['parameters']['dc'] == pytest.approx(3.3)


def test_duplicate_instances_are_rejected():
    with pytest.raises(ProfileTestbenchError, match='duplicate instance'):
        confirm_profile_netlist(
            profile('stability', 'unity_gain_stability', {'name': 'loop', 'type': 'stb'}),
            STB_NETLIST + '\nIPRB (VOUT VINN) iprobe\n',
            STB_EXPECTATION,
        )

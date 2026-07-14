'''Structural confirmation for independently exported verification profiles.'''

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from analog_opt.profiles import VerificationProfileConfig


class ProfileTestbenchError(ValueError):
    '''Raised when an exported profile netlist does not match its evidence.'''


@dataclass(frozen=True)
class ProfileNetlistConfirmation:
    profile_id: str
    dut_instance: str
    dut_cell: str
    dut_nodes: Tuple[str, ...]
    instances: Mapping[str, Mapping[str, Any]]
    analyses: Mapping[str, Mapping[str, Any]]
    probe: Optional[Mapping[str, str]]
    pulse: Optional[Mapping[str, Any]]
    netlist_sha256: str


_INSTANCE = re.compile(r'^([A-Za-z_][A-Za-z0-9_.$-]*)\s*\(([^)]*)\)\s*(\S+)(?:\s+(.*))?$')
_ANALYSIS = re.compile(r'^([A-Za-z_][A-Za-z0-9_.$-]*)\s+(dc|ac|noise|tran|stb)\b(?:\s+(.*))?$', re.I)
_NUMBER = re.compile(r'^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(meg|[TtGgKkMmUuNnPpFf]?)$')
_SUFFIX = {
    't': 1e12, 'g': 1e9, 'meg': 1e6, 'k': 1e3, 'm': 1e-3,
    'u': 1e-6, 'n': 1e-9, 'p': 1e-12, 'f': 1e-15,
}


def _logical_lines(text: str) -> tuple:
    if not isinstance(text, str) or not text.strip():
        raise ProfileTestbenchError('profile netlist text is empty')
    lines = []
    pending = ''
    for raw in text.splitlines():
        stripped = raw.strip()
        if pending:
            stripped = pending + ' ' + stripped
        if stripped.endswith('\\'):
            pending = stripped[:-1].rstrip()
            continue
        if stripped:
            lines.append(stripped)
        pending = ''
    if pending:
        raise ProfileTestbenchError('unterminated netlist line continuation')
    return tuple(lines)


def _value(token: str) -> Any:
    candidate = token.strip().strip('()')
    match = _NUMBER.fullmatch(candidate)
    if match is None:
        return candidate
    magnitude, suffix = match.groups()
    return float(magnitude) * (_SUFFIX[suffix.lower()] if suffix else 1.0)


def _parameters(text: Optional[str], location: str) -> dict:
    result = {}
    if not text:
        return result
    for token in text.split():
        if '=' not in token:
            raise ProfileTestbenchError(location + ' has unsupported token ' + token)
        name, raw_value = token.split('=', 1)
        if not name or name in result:
            raise ProfileTestbenchError(location + ' has duplicate or empty parameter')
        result[name] = _value(raw_value)
    return result


def _parse(text: str) -> Tuple[dict, dict]:
    instances = {}
    analyses = {}
    ignored = ('simulator ', 'include ', 'global ', 'parameters ', '//', '*')
    for line in _logical_lines(text):
        if line.lower().startswith(ignored):
            continue
        instance_match = _INSTANCE.fullmatch(line)
        if instance_match is not None:
            name, nodes, model, parameter_text = instance_match.groups()
            if name in instances:
                raise ProfileTestbenchError('duplicate instance: ' + name)
            instances[name] = {
                'model': model,
                'nodes': tuple(nodes.split()),
                'parameters': _parameters(parameter_text, 'instance ' + name),
            }
            continue
        analysis_match = _ANALYSIS.fullmatch(line)
        if analysis_match is not None:
            name, analysis_type, parameter_text = analysis_match.groups()
            if name in analyses:
                raise ProfileTestbenchError('duplicate analysis: ' + name)
            analyses[name] = {
                'type': analysis_type.lower(),
                'parameters': _parameters(parameter_text, 'analysis ' + name),
            }
            continue
        raise ProfileTestbenchError('unsupported profile netlist line: ' + line)
    return instances, analyses


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)):
        return not isinstance(actual, bool) and isinstance(actual, (int, float)) and math.isfinite(float(actual)) and math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-15)
    return str(actual).lower() == str(expected).lower()


def _confirm_entry(kind: str, name: str, actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    if kind == 'instance':
        expected_model = expected.get('model')
        if expected_model is not None and not _same(actual.get('model'), expected_model):
            raise ProfileTestbenchError(name + ' model mismatch')
        expected_nodes = expected.get('nodes')
        if expected_nodes is not None and tuple(actual.get('nodes', ())) != tuple(expected_nodes):
            raise ProfileTestbenchError(name + ' nodes mismatch')
    else:
        expected_type = expected.get('type')
        if expected_type is not None and not _same(actual.get('type'), expected_type):
            raise ProfileTestbenchError(name + ' analysis type mismatch')
    actual_parameters = actual.get('parameters', {})
    expected_parameters = expected.get('parameters', {})
    if not isinstance(expected_parameters, Mapping):
        raise ProfileTestbenchError(name + ' expected parameters must be a mapping')
    for parameter, expected_value in expected_parameters.items():
        if parameter not in actual_parameters or not _same(actual_parameters[parameter], expected_value):
            raise ProfileTestbenchError(name + ' parameter ' + parameter + ' mismatch')


def confirm_profile_netlist(
    profile: VerificationProfileConfig,
    netlist_text: str,
    expectation: Mapping[str, Any],
) -> ProfileNetlistConfirmation:
    '''Prove DUT, sources, load, probe orientation, and analyses structurally.'''
    if not isinstance(profile, VerificationProfileConfig):
        raise ProfileTestbenchError('profile must be VerificationProfileConfig')
    if not isinstance(expectation, Mapping):
        raise ProfileTestbenchError('profile expectation must be a mapping')
    instances, analyses = _parse(netlist_text)
    dut = instances.get(profile.dut_instance)
    if dut is None:
        raise ProfileTestbenchError('DUT instance is missing: ' + profile.dut_instance)
    dut_cell = expectation.get('dut_cell')
    if not isinstance(dut_cell, str) or not dut_cell:
        raise ProfileTestbenchError('expected DUT cell is missing')
    if not _same(dut['model'], dut_cell):
        raise ProfileTestbenchError('DUT cell mismatch')
    expected_instances = expectation.get('instances', {})
    expected_analyses = expectation.get('analyses', {})
    if not isinstance(expected_instances, Mapping) or not isinstance(expected_analyses, Mapping):
        raise ProfileTestbenchError('profile instance and analysis expectations must be mappings')
    for name, expected in expected_instances.items():
        actual = instances.get(name)
        if actual is None:
            raise ProfileTestbenchError('required instance is missing: ' + name)
        _confirm_entry('instance', name, actual, expected)
    for name, expected in expected_analyses.items():
        actual = analyses.get(name)
        if actual is None:
            raise ProfileTestbenchError('required analysis is missing: ' + name)
        _confirm_entry('analysis', name, actual, expected)
    probe = expectation.get('probe')
    if profile.role == 'unity_gain_stability':
        if not isinstance(probe, Mapping):
            raise ProfileTestbenchError('stability profile requires probe evidence')
        probe_name = probe.get('instance')
        actual_probe = instances.get(probe_name)
        if actual_probe is None or len(actual_probe['nodes']) != 2:
            raise ProfileTestbenchError('stability probe instance is invalid')
        observed_probe = {
            'instance': probe_name,
            'plus': actual_probe['nodes'][0],
            'minus': actual_probe['nodes'][1],
        }
        if observed_probe != dict(probe):
            raise ProfileTestbenchError(str(probe_name) + ' nodes mismatch')
        if not any(item['type'] == 'stb' and item['parameters'].get('probe') == probe_name for item in analyses.values()):
            raise ProfileTestbenchError('STB analysis does not reference the confirmed probe')
        probe = observed_probe
    elif probe is not None:
        probe = dict(probe)
    pulse = expectation.get('pulse')
    if profile.role == 'unity_gain_slew':
        if not isinstance(pulse, Mapping):
            raise ProfileTestbenchError('slew profile requires pulse evidence')
        pulse_name = pulse.get('instance')
        actual_pulse = instances.get(pulse_name)
        if actual_pulse is None:
            raise ProfileTestbenchError('slew pulse instance is missing')
        if not _same(actual_pulse['parameters'].get('type'), 'pulse'):
            raise ProfileTestbenchError('slew source is not pulse')
        if not _same(actual_pulse['parameters'].get('val0'), pulse.get('low')) or not _same(actual_pulse['parameters'].get('val1'), pulse.get('high')):
            raise ProfileTestbenchError('slew pulse levels mismatch')
        if not any(item['type'] == 'tran' for item in analyses.values()):
            raise ProfileTestbenchError('slew profile requires transient analysis')
        pulse = dict(pulse)
    elif pulse is not None:
        pulse = dict(pulse)
    return ProfileNetlistConfirmation(
        profile_id=profile.id,
        dut_instance=profile.dut_instance,
        dut_cell=dut_cell,
        dut_nodes=tuple(dut['nodes']),
        instances=instances,
        analyses=analyses,
        probe=dict(probe) if isinstance(probe, Mapping) else None,
        pulse=dict(pulse) if isinstance(pulse, Mapping) else None,
        netlist_sha256=hashlib.sha256(netlist_text.encode('utf-8')).hexdigest(),
    )

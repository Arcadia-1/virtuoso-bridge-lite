'''Verification profile data model for analog optimization V2.'''

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, Tuple

from analog_opt.evaluator import atomic_write_json


@dataclass(frozen=True)
class VerificationProfileConfig:
    '''One independently netlisted and simulated verification testbench.'''

    id: str
    role: str
    testbench_cell: str
    dut_instance: str
    stimuli: Mapping[str, Mapping[str, Any]]
    analyses: Tuple[Mapping[str, Any], ...]
    metrics: Tuple[Mapping[str, Any], ...]
    specs: Tuple[Mapping[str, Any], ...]
    pvt_policy: str = 'full'
    timeout_s: int = 1800


@dataclass(frozen=True)
class ProfileRuntime:
    '''Executable boundary for one already-prepared verification profile.'''

    id: str
    evaluate: Callable[[Mapping[str, float], Path, Mapping[str, Any]], Mapping[str, Any]]
    required: bool = True


class MultiProfileError(ValueError):
    '''Raised when profile orchestration state or protocol is invalid.'''


_PROFILE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')


def _candidate(candidate: Mapping[str, float]) -> dict:
    if not isinstance(candidate, Mapping):
        raise MultiProfileError('candidate must be a mapping')
    result = {}
    for name, value in candidate.items():
        if not isinstance(name, str) or not name:
            raise MultiProfileError('candidate names must be nonempty strings')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise MultiProfileError('candidate values must be finite numbers')
        result[name] = float(value)
    return result


def _candidate_hash(candidate: Mapping[str, float]) -> str:
    payload = json.dumps(candidate, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _profile_result(value: Any, profile_id: str) -> dict:
    if not isinstance(value, Mapping):
        raise MultiProfileError('profile backend must return a mapping')
    success = value.get('success', True)
    objective = value.get('objective')
    if type(success) is not bool:
        raise MultiProfileError('profile success must be exactly bool')
    if isinstance(objective, bool) or not isinstance(objective, (int, float)) or not math.isfinite(float(objective)):
        raise MultiProfileError('profile objective must be finite')
    metrics = value.get('metrics', {})
    specs = value.get('specs', {})
    metadata = value.get('metadata', {})
    if not isinstance(metrics, Mapping) or not isinstance(specs, Mapping) or not isinstance(metadata, Mapping):
        raise MultiProfileError('profile metrics, specs, and metadata must be mappings')
    failure = value.get('failure')
    if success is False:
        if not isinstance(failure, Mapping) or not isinstance(failure.get('message'), str):
            raise MultiProfileError('failed profile must include failure.message')
    elif failure is not None:
        raise MultiProfileError('successful profile cannot include failure')
    return {
        'profile_id': profile_id, 'objective': float(objective), 'success': success,
        'metrics': dict(metrics), 'specs': dict(specs), 'metadata': dict(metadata),
        'failure': dict(failure) if isinstance(failure, Mapping) else None,
    }


class MultiProfileBackend:
    '''Apply one candidate once, then evaluate independent profile runtimes.'''

    def __init__(
        self,
        apply_candidate: Callable[[Mapping[str, float]], Any],
        runtimes: Sequence[ProfileRuntime],
        *,
        failure_penalty: float = 1e30,
    ) -> None:
        if not callable(apply_candidate):
            raise MultiProfileError('apply_candidate must be callable')
        self.apply_candidate = apply_candidate
        self.runtimes = tuple(runtimes)
        if not self.runtimes:
            raise MultiProfileError('at least one profile runtime is required')
        ids = []
        for runtime in self.runtimes:
            if not isinstance(runtime, ProfileRuntime) or _PROFILE_ID_RE.fullmatch(runtime.id) is None:
                raise MultiProfileError('profile runtime id is invalid')
            ids.append(runtime.id)
        if len(set(ids)) != len(ids):
            raise MultiProfileError('profile runtime ids must be unique')
        if isinstance(failure_penalty, bool) or not isinstance(failure_penalty, (int, float)) or not math.isfinite(float(failure_penalty)) or float(failure_penalty) <= 0:
            raise MultiProfileError('failure_penalty must be finite and positive')
        self.failure_penalty = float(failure_penalty)

    def _failure(self, candidate: dict, profiles: Mapping[str, Any], profile_id: str, stage: str, message: str) -> dict:
        detail = {'profile_id': profile_id, 'stage': stage, 'message': str(message)}
        return {
            'objective': self.failure_penalty, 'success': False,
            'metrics': {}, 'specs': {},
            'failure': {'category': 'profile', 'message': '%s %s failed: %s' % (profile_id, stage, message)},
            'metadata': {
                'physical_candidate': candidate, 'profiles': dict(profiles),
                'failure_detail': detail,
            },
        }

    def __call__(
        self,
        candidate: Mapping[str, float],
        directory: Path,
        conditions: Mapping[str, Any] = None,
    ) -> dict:
        physical = _candidate(candidate)
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        state_path = root / 'profile_state.json'
        candidate_hash = _candidate_hash(physical)
        runtime_ids = [runtime.id for runtime in self.runtimes]
        completed = {}
        resumed = []
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding='utf-8'))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise MultiProfileError('invalid profile resume state: %s' % exc) from exc
            if not isinstance(state, Mapping) or state.get('version') != 1:
                raise MultiProfileError('invalid profile resume state schema')
            if state.get('candidate_hash') != candidate_hash or state.get('profile_ids') != runtime_ids:
                raise MultiProfileError('profile resume state does not match candidate or profiles')
            if state.get('status') == 'complete':
                raise MultiProfileError('profile evaluation directory is already complete')
            raw_completed = state.get('completed', {})
            if not isinstance(raw_completed, Mapping):
                raise MultiProfileError('profile resume completed results are invalid')
            completed = {str(name): _profile_result(value, str(name)) for name, value in raw_completed.items()}
            resumed = [name for name in runtime_ids if name in completed]
        state = {
            'version': 1, 'status': 'in_progress', 'candidate_hash': candidate_hash,
            'profile_ids': runtime_ids, 'completed': completed,
        }
        atomic_write_json(state_path, state)
        try:
            self.apply_candidate(physical)
        except Exception as exc:
            state['status'] = 'failed'; state['failure'] = {'stage': 'apply', 'message': str(exc)}
            atomic_write_json(state_path, state)
            return self._failure(physical, completed, 'candidate', 'apply', str(exc))
        conditions = dict(conditions or {})
        for runtime in self.runtimes:
            if runtime.id in completed:
                continue
            profile_dir = root / 'profiles' / runtime.id
            profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = _profile_result(runtime.evaluate(physical, profile_dir, conditions), runtime.id)
                if result['success'] is False:
                    raise MultiProfileError(result['failure']['message'])
            except Exception as exc:
                if runtime.required:
                    state['status'] = 'failed'
                    state['failure'] = {'profile_id': runtime.id, 'stage': 'evaluation', 'message': str(exc)}
                    atomic_write_json(state_path, state)
                    return self._failure(physical, completed, runtime.id, 'evaluation', str(exc))
                result = {
                    'profile_id': runtime.id, 'objective': 0.0, 'success': False,
                    'metrics': {}, 'specs': {}, 'metadata': {},
                    'failure': {'category': 'profile', 'message': str(exc)},
                }
            completed[runtime.id] = result
            state['completed'] = completed
            state['status'] = 'in_progress'
            state.pop('failure', None)
            atomic_write_json(state_path, state)
        metrics = {}
        specs = {}
        objective = 0.0
        profile_metadata = {}
        for runtime in self.runtimes:
            result = completed[runtime.id]
            for name, value in result['metrics'].items():
                if name in metrics:
                    return self._failure(physical, completed, runtime.id, 'aggregation', 'duplicate metric '+name)
                metrics[name] = value
            for name, value in result['specs'].items():
                if name in specs:
                    return self._failure(physical, completed, runtime.id, 'aggregation', 'duplicate spec '+name)
                specs[name] = value
            if result['success']:
                objective += result['objective']
            profile_metadata[runtime.id] = result['metadata']
        state['status'] = 'complete'
        state['completed'] = completed
        atomic_write_json(state_path, state)
        return {
            'objective': float(objective), 'success': True,
            'metrics': metrics, 'specs': specs, 'failure': None,
            'metadata': {
                'physical_candidate': physical, 'profiles': profile_metadata,
                'resumed_profiles': resumed, 'profile_state': str(state_path),
            },
        }

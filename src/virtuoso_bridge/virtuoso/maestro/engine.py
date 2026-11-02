"""Spectre engine/preset configuration of an existing Maestro test, not a run."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.skill_output import is_single_complete_skill_list, parse_sexpr

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient

Engine = Literal["spectre", "aps", "spectre_x"]
Preset = Literal["CX", "AX", "MX", "LX", "VX"]
_MODES = {"spectre": "Spectre", "aps": "APS", "spectre_x": "Spectre X"}
_PRESETS = ("CX", "AX", "MX", "LX", "VX")
_TAG = "VB_MAESTRO_ENGINE_V1"


class MaestroEngineError(RuntimeError):
    """Engine configuration failed; inspect result and snapshots before acting."""

    def __init__(self, message, *, result=None, phase=None, before=None, after=None):
        super().__init__(message)
        self.result = result
        self.phase = phase
        self.before = before
        self.after = after


class MaestroEngineOutcomeUnknown(MaestroEngineError):
    """A mutation may have executed. Reconcile it; do not automatically retry."""


class MaestroEngineConfig(BaseModel):
    """Active mode plus the stored X preset (which is inactive outside X)."""

    model_config = ConfigDict(frozen=True)
    test: str
    session: str
    engine: Engine
    preset: Preset | None
    stored_spectre_x_preset: str | None


class MaestroEngineChangeResult(BaseModel):
    """Verified in-memory state; persistence requires a separate explicit save."""

    model_config = ConfigDict(frozen=True)
    before: MaestroEngineConfig
    after: MaestroEngineConfig
    changed: bool
    saved: Literal[False] = False


def _text(value: str, label: str, *, empty=False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or (value and not value.strip()):
        raise ValueError(f"{label} must be a nonempty string" if not empty else f"invalid {label}")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} cannot contain control characters")
    return escape_skill_string(value)


def _selection(engine: str, preset: str | None) -> tuple[str, str | None]:
    if not isinstance(engine, str) or engine.lower() not in _MODES:
        raise ValueError("engine must be spectre, aps or spectre_x")
    engine = engine.lower()
    if preset is not None:
        if not isinstance(preset, str) or preset.upper() not in _PRESETS:
            raise ValueError("preset must be CX, AX, MX, LX or VX")
        preset = preset.upper()
    if engine == "spectre_x" and preset is None:
        raise ValueError("spectre_x requires an explicit preset")
    if engine != "spectre_x" and preset is not None:
        raise ValueError("preset applies only to spectre_x")
    return engine, preset


def _engine_skill(test: str, session: str, engine=None, preset=None) -> str:
    test, session = _text(test, "test"), _text(session, "session", empty=True)
    target = f'maeGetTestSession("{test}"' + (f' ?session "{session}"' if session else "") + ')'
    required = ["maeGetTestSession", "asiGetAnalogSimulator", "asiGetHighPerformanceOptionVal"]
    if engine is not None:
        required.append("asiSetHighPerformanceOptionVal")
    # Every structured failure has the same envelope, retaining partial evidence.
    def fail(status, phase):
        return f'return(list("{_TAG}" "{status}" "{phase}" vbBefore vbAfter))'

    code = 'prog((vbTest vbProbe vbMode vbPresetProbe vbBefore vbAfter vbFailure vbPhase) '
    for function in required:
        code += f'unless(isCallable(\'{function}) {fail("unsupported", function)}) '
    code += (
        f'vbProbe=errset({target} nil) '
        f'unless(vbProbe && car(vbProbe) {fail("target_not_found", "maeGetTestSession")}) '
        'vbTest=car(vbProbe) vbProbe=errset(asiGetAnalogSimulator(vbTest) nil) '
        f'unless(vbProbe && member(car(vbProbe) list(\'spectre \'Spectre "spectre" "Spectre")) '
        f'{fail("unsupported", "spectre_backend")}) '
        'vbProbe=errset(asiGetHighPerformanceOptionVal(vbTest \'uniMode) nil) '
        f'unless(vbProbe && member(car(vbProbe) list("Spectre" "APS" "Spectre X")) '
        f'{fail("unsupported", "uniMode")}) '
        'vbMode=car(vbProbe) '
        'vbPresetProbe=errset(asiGetHighPerformanceOptionVal(vbTest \'spectreXPreset) nil) '
        'vbBefore=list(vbMode car(vbPresetProbe)) vbAfter=vbBefore '
    )
    if engine is None:
        return code + f'return(list("{_TAG}" "ok" "read" vbBefore vbAfter)))'
    # X must be understood both when entering it and when leaving it: otherwise
    # a write could succeed before Python rejects an unrepresentable old state.
    preset_required = 't' if engine == "spectre_x" else 'vbMode == "Spectre X"'
    code += (f'when({preset_required} '
             f'unless(vbPresetProbe && member(car(vbPresetProbe) list("CX" "AX" "MX" "LX" "VX")) '
             f'{fail("unsupported", "spectreXPreset")})) ')
    # Set the dormant preset first so an unsupported preset cannot switch the mode.
    assignments = []
    if preset is not None:
        assignments.append(("spectreXPreset", preset, 'cadr(vbBefore)'))
    assignments.append(("uniMode", _MODES[engine], 'car(vbBefore)'))
    for option, value, old in assignments:
        code += (
            f'when(!vbFailure && {old} != "{value}" '
            f'vbPhase="{option}" '
            f'vbProbe=errset(asiSetHighPerformanceOptionVal(vbTest \'{option} "{value}") nil) '
            f'unless(vbProbe && car(vbProbe) == "{value}" vbFailure=t)) '
        )
    code += (
        'vbProbe=errset(list(asiGetHighPerformanceOptionVal(vbTest \'uniMode) '
        'car(errset(asiGetHighPerformanceOptionVal(vbTest \'spectreXPreset) nil))) nil) '
        'vbAfter=when(vbProbe car(vbProbe)) '
        f'unless(vbAfter {fail("readback_failed", "readback")}) '
        f'when(vbFailure return(list("{_TAG}" "write_failed" vbPhase vbBefore vbAfter))) '
        f'unless(car(vbAfter) == "{_MODES[engine]}" '
        + (f'&& cadr(vbAfter) == "{preset}" ' if preset else '')
        + f'{fail("mismatch", "readback")}) '
        + f'return(list("{_TAG}" "ok" "verified" vbBefore vbAfter)))'
    )
    return code


def _config(snapshot, test, session):
    if not isinstance(snapshot, list) or len(snapshot) != 2:
        raise ValueError("malformed engine snapshot")
    mode, raw_preset = snapshot
    reverse = {v: k for k, v in _MODES.items()}
    if not isinstance(mode, str) or mode not in reverse or (raw_preset is not None and not isinstance(raw_preset, str)):
        raise ValueError("unsupported or malformed engine snapshot")
    if mode == "Spectre X" and raw_preset not in _PRESETS:
        raise ValueError("unsupported Spectre X preset")
    return MaestroEngineConfig(test=test, session=session, engine=reverse[mode],
                               preset=raw_preset if mode == "Spectre X" else None,
                               stored_spectre_x_preset=raw_preset)


def _execute(client, code, *, test, session, mutation, timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    result = None
    error_type = MaestroEngineOutcomeUnknown if mutation else MaestroEngineError
    try:
        result = client.execute_skill(code, timeout=timeout, retry_connect=False)
    except Exception as exc:
        raise error_type("Engine request failed; do not blindly retry a mutation.",
                         result=getattr(exc, "result", None)) from exc
    if not result.ok or result.errors:
        if mutation and (result.metadata.get("request_sent") is False or result.metadata.get("outcome") == "not_started"):
            error_type = MaestroEngineError
        raise error_type("Engine request was not confirmed: " + "; ".join(result.errors), result=result)
    raw = (result.output or "").strip()
    if not is_single_complete_skill_list(raw):
        raise error_type("Malformed engine response; inspect the original request.", result=result)
    data = parse_sexpr(raw)
    if len(data) != 5 or data[0] != _TAG:
        raise error_type("Unsupported engine response envelope.", result=result)
    _, status, phase, before, after = data
    if status != "ok":
        cls = MaestroEngineOutcomeUnknown if mutation and status == "readback_failed" else MaestroEngineError
        raise cls(f"Maestro engine {status}: {phase}.", result=result, phase=phase, before=before, after=after)
    try:
        return _config(before, test, session), _config(after, test, session), result
    except ValueError as exc:
        raise error_type(str(exc), result=result, before=before, after=after) from exc


def get_engine(client: VirtuosoClient, test: str, *, session: str = "", timeout: float = 30) -> MaestroEngineConfig:
    """Read an existing Spectre test; never open, save, run or close a session."""
    code = _engine_skill(test, session)
    return _execute(client, code, test=test, session=session, mutation=False, timeout=timeout)[1]


def set_engine(
    client: VirtuosoClient, test: str, *, engine: Engine, preset: Preset | None = None,
    session: str = "", timeout: float = 30,
) -> MaestroEngineChangeResult:
    """Set only uniMode and (for X) spectreXPreset, then verify once in memory.

Use an idle, editable setup. X requires an explicit preset. The call does not
save, netlist, run, manage session lifecycle or provide atomic rollback. A
partial failure retains before/after snapshots; uncertain transport retains
the result/request handle. No mutation is automatically repeated.
"""
    engine, preset = _selection(engine, preset)
    code = _engine_skill(test, session, engine, preset)
    before, after, result = _execute(client, code, test=test, session=session, mutation=True, timeout=timeout)
    if after.engine != engine or (preset is not None and after.preset != preset):
        raise MaestroEngineError("Requested engine/preset did not match readback.", result=result, before=before, after=after)
    return MaestroEngineChangeResult(before=before, after=after, changed=before != after)

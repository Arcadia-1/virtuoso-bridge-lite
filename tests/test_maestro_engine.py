"""Offline protocol and SKILL generation tests, not native ASI emulation."""

import math

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.maestro import (
    get_engine, set_engine, MaestroEngineConfig, MaestroEngineError,
    MaestroEngineOutcomeUnknown,
)
from virtuoso_bridge.virtuoso.maestro.engine import _engine_skill


def response(before='("APS" "MX")', after=None, status="ok", phase="verified"):
    return VirtuosoResult(status=ExecutionStatus.SUCCESS,
        output=f'("VB_MAESTRO_ENGINE_V1" "{status}" "{phase}" {before} {after or before})')


class Client:
    def __init__(self, result=None):
        self.result = result if result is not None else response()
        self.calls = []

    def execute_skill(self, code, **kwargs):
        self.calls.append((code, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_reader_hides_inactive_preset_but_retains_stored_value():
    client = Client()
    config = get_engine(client, "TRAN", session="S1")
    assert isinstance(config, MaestroEngineConfig)
    assert config.engine == "aps" and config.preset is None
    assert config.stored_spectre_x_preset == "MX"
    assert config.test == "TRAN" and config.session == "S1"
    assert "asiSetHighPerformance" not in client.calls[0][0]


@pytest.mark.parametrize("engine,raw", [("spectre", "Spectre"), ("aps", "APS")])
def test_non_x_engine_does_not_write_preset(engine, raw):
    client = Client(response(after=f'("{raw}" "MX")'))
    result = set_engine(client, "T", engine=engine)
    assert result.after.engine == engine and result.after.preset is None
    assert result.saved is False
    code, kwargs = client.calls[0]
    assert "asiSetHighPerformanceOptionVal(vbTest 'spectreXPreset" not in code
    assert kwargs == dict(timeout=30, retry_connect=False)


@pytest.mark.parametrize("preset", ["CX", "AX", "MX", "LX", "VX"])
def test_x_presets_are_explicit_and_verified(preset):
    client = Client(response(after=f'("Spectre X" "{preset}")'))
    result = set_engine(client, "T", engine="SPECTRE_X", preset=preset.lower())
    assert result.after.preset == preset and result.changed
    assert len(client.calls) == 1
    code = client.calls[0][0]
    assert code.index("asiSetHighPerformanceOptionVal(vbTest 'spectreXPreset") < code.index("asiSetHighPerformanceOptionVal(vbTest 'uniMode")
    for forbidden in ("maeOpenSetup", "maeCloseSession", "maeSaveSetup", "maeRunSimulation", "maeCreateTest", "mtOption", "dbSave", "hiDisplay"):
        assert forbidden not in code


@pytest.mark.parametrize("engine,preset", [("bad", None), (None, None), ("spectre_x", None), ("aps", "AX"), ("spectre", "MX"), ("spectre_x", "bad"), ("spectre_x", 1)])
def test_invalid_combinations_never_send(engine, preset):
    client = Client()
    with pytest.raises(ValueError):
        set_engine(client, "T", engine=engine, preset=preset)
    assert client.calls == []


@pytest.mark.parametrize("test,session", [("", ""), ("T\n", ""), ("T", "\x00"), (None, ""), ("T", None)])
def test_invalid_target_never_sends(test, session):
    client = Client()
    with pytest.raises(ValueError):
        get_engine(client, test, session=session)
    assert client.calls == []


def test_names_are_escaped_and_target_is_explicit_when_supplied():
    code = _engine_skill('T"\\', 'S"\\')
    assert 'maeGetTestSession("T\\"\\\\" ?session "S\\"\\\\")' in code
    assert 'maeGetTestSession("T")' in _engine_skill("T", "")
    assert "asiGetCurrentSession" not in code


def test_same_config_is_noop_and_skips_matching_setters_in_skill():
    client = Client()
    result = set_engine(client, "T", engine="aps")
    assert result.changed is False
    assert 'when(!vbFailure && car(vbBefore) != "APS"' in client.calls[0][0]


@pytest.mark.parametrize("status,phase", [("unsupported", "asiSetHighPerformanceOptionVal"), ("target_not_found", "maeGetTestSession"), ("unsupported", "spectre_backend"), ("unsupported", "spectreXPreset")])
def test_capability_and_target_failures_have_evidence(status, phase):
    client = Client(response(status=status, phase=phase))
    with pytest.raises(MaestroEngineError) as exc:
        set_engine(client, "T", engine="aps")
    assert exc.value.phase == phase and exc.value.result is client.result
    assert len(client.calls) == 1


def test_partial_write_retains_snapshots_without_rollback_or_retry():
    client = Client(response(after='("APS" "AX")', status="write_failed", phase="uniMode"))
    with pytest.raises(MaestroEngineError) as exc:
        set_engine(client, "T", engine="spectre_x", preset="AX")
    assert exc.value.before == ["APS", "MX"]
    assert exc.value.after == ["APS", "AX"]
    assert len(client.calls) == 1


def test_successful_setter_but_wrong_readback_is_failure():
    client = Client(response(status="mismatch", phase="readback"))
    with pytest.raises(MaestroEngineError):
        set_engine(client, "T", engine="spectre_x", preset="AX")
    assert len(client.calls) == 1


def test_envelope_cannot_claim_success_with_wrong_requested_value():
    client = Client(response())
    with pytest.raises(MaestroEngineError) as exc:
        set_engine(client, "T", engine="spectre_x", preset="AX")
    assert exc.value.result is client.result
    assert len(client.calls) == 1


def test_missing_api_is_checked_before_any_mutation():
    code = _engine_skill("T", "S", "spectre_x", "AX")
    assert code.index("isCallable('asiSetHighPerformanceOptionVal)") < code.index('vbTest=car')
    assert code.index('"unsupported" "spectreXPreset"') < code.index("asiSetHighPerformanceOptionVal(vbTest")


def test_readback_failure_after_mutation_is_unknown_with_snapshots():
    client = Client(response(after="nil", status="readback_failed", phase="readback"))
    with pytest.raises(MaestroEngineOutcomeUnknown) as exc:
        set_engine(client, "T", engine="aps")
    assert exc.value.before == ["APS", "MX"] and exc.value.after is None
    assert exc.value.result is client.result


def test_errors_are_not_hidden_by_success_status():
    client = Client(VirtuosoResult(status=ExecutionStatus.SUCCESS, errors=["SKILL error"], output="nil"))
    with pytest.raises(MaestroEngineError):
        get_engine(client, "T")


@pytest.mark.parametrize("result", [TimeoutError("timeout"), OSError("connection lost"),
    VirtuosoResult(status=ExecutionStatus.ERROR, errors=["socket timeout"], metadata={"request_handle": {"id": "original"}}),
    VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")])
def test_unknown_mutation_is_never_repeated_and_retains_original_receipt(result):
    client = Client(result)
    with pytest.raises(MaestroEngineOutcomeUnknown) as exc:
        set_engine(client, "T", engine="aps")
    assert len(client.calls) == 1
    if isinstance(result, VirtuosoResult):
        assert exc.value.result is result


def test_guard_not_sent_is_known_failure_not_unknown_mutation():
    client = Client(VirtuosoResult(status=ExecutionStatus.ERROR, errors=["blocked"],
        metadata={"request_sent": False, "dialog_guard": {"status": "blocked"}}))
    with pytest.raises(MaestroEngineError) as exc:
        set_engine(client, "T", engine="aps")
    assert not isinstance(exc.value, MaestroEngineOutcomeUnknown)
    assert exc.value.result.metadata["dialog_guard"]["status"] == "blocked"


@pytest.mark.parametrize("raw", ['("VB_MAESTRO_ENGINE_V1" "ok" "read" ("Spectre FX" nil) ("Spectre FX" nil))',
    '("VB_MAESTRO_ENGINE_V1" "ok" "read" ("Spectre X" "bad") ("Spectre X" "bad"))', '(nil)', 'nil'])
def test_unsupported_or_malformed_read_does_not_guess_a_default(raw):
    client = Client(VirtuosoResult(status=ExecutionStatus.SUCCESS, output=raw))
    with pytest.raises(MaestroEngineError):
        get_engine(client, "T")


@pytest.mark.parametrize("timeout", [0, -1, math.inf, math.nan, True, "30"])
def test_invalid_timeout_never_sends(timeout):
    client = Client()
    with pytest.raises(ValueError):
        set_engine(client, "T", engine="aps", timeout=timeout)
    assert client.calls == []

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.maestro import (
    add_output,
    create_netlist_for_corner,
    run_and_wait,
    run_simulation,
)
from virtuoso_bridge.virtuoso.maestro import MaestroOps
from virtuoso_bridge.virtuoso.maestro import writer
from virtuoso_bridge.virtuoso.maestro.writer import _without_history_name_form
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.dialogs import DialogBlockedError
from virtuoso_bridge.virtuoso.requests import RequestHandle, RequestRecoveryError


class _RecordingClient:
    def __init__(self) -> None:
        self.expressions: list[str] = []
        self.skill_kwargs: list[dict] = []
        self.ssh_runner = None

    def execute_skill(self, expression: str, **kwargs):
        self.expressions.append(expression)
        self.skill_kwargs.append(kwargs)
        return SimpleNamespace(errors=[], output="t")


def test_create_netlist_for_corner_uses_current_session_by_default() -> None:
    client = _RecordingClient()

    result = create_netlist_for_corner(
        client,
        "tran_test",
        "tt",
        "/tmp/tran_tt",
    )

    assert result == "t"
    assert client.expressions == [
        'maeCreateNetlistForCorner("tran_test" "tt" "/tmp/tran_tt")'
    ]


def test_add_output_escapes_all_skill_string_parameters() -> None:
    client = _RecordingClient()

    add_output(
        client,
        'Pin"\\HB',
        'hb"\\1tone',
        output_type='point"\\type',
        signal_name='VIN"\\PLUS',
        expr='harmonic(pvi(\'hb, "\\/IN_PORT", 0, "/VIN/PLUS", 0), 1)',
        session='session"\\3',
    )

    assert client.expressions == [
        'maeAddOutput("Pin\\"\\\\HB" "hb\\"\\\\1tone" '
        '?outputType "point\\"\\\\type" ?signalName "VIN\\"\\\\PLUS" '
        '?expr "harmonic(pvi(\'hb, \\"\\\\/IN_PORT\\", 0, \\"/VIN/PLUS\\", 0), 1)" '
        '?session "session\\"\\\\3")'
    ]


def test_run_simulation_forwards_timeout_to_skill_request() -> None:
    client = _RecordingClient()

    run_simulation(client, session='session"\\3', callback='done"\\callback', timeout=90)

    assert client.expressions == [
        _without_history_name_form(
            'maeRunSimulation(?session "session\\"\\\\3" ?callback "done\\"\\\\callback")'
        )
    ]
    assert client.skill_kwargs == [{"timeout": 90}]


def test_run_simulation_passes_explicit_run_mode() -> None:
    client = _RecordingClient()

    run_simulation(
        client,
        session="session4",
        callback="done",
        run_mode='Monte Carlo "Sampling"',
    )

    assert client.expressions == [
        _without_history_name_form(
            'maeRunSimulation(?session "session4" ?callback "done" '
            '?runMode "Monte Carlo \\"Sampling\\"")'
        )
    ]


def test_run_and_wait_uses_one_timeout_budget(monkeypatch) -> None:
    client = _RecordingClient()
    clock = [100.0]
    start_timeouts: list[float] = []
    wait_timeouts: list[float] = []

    def fake_run_simulation(*args, **kwargs):
        start_timeouts.append(kwargs["timeout"])
        clock[0] += 12
        return '"Interactive.1"'

    def fake_wait(*args, **kwargs):
        wait_timeouts.append(kwargs["timeout"])
        return "done"

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(writer, "run_simulation", fake_run_simulation)
    monkeypatch.setattr(writer, "_wait_until_done", fake_wait)
    monkeypatch.setattr(writer.uuid, "uuid4", lambda: SimpleNamespace(hex="deadbeef"))

    history, status = run_and_wait(client, run_mode="Monte Carlo Sampling", timeout=60)

    assert (history, status) == ('"Interactive.1"', "done")
    assert start_timeouts == [60]
    assert wait_timeouts == [48]


def test_completion_wait_timeout_keeps_acknowledged_run(monkeypatch) -> None:
    client = _RecordingClient()
    monkeypatch.setattr(writer, "run_simulation", lambda *a, **kw: '"Interactive.11"')
    monkeypatch.setattr(writer, "_wait_until_done", lambda *a, **kw:
                        (_ for _ in ()).throw(TimeoutError("completion timed out")))
    monkeypatch.setattr(writer.uuid, "uuid4", lambda: SimpleNamespace(hex="deadbeef"))
    # Existing callers catch TimeoutError; the recovery receipt is additive.
    with pytest.raises(TimeoutError) as caught:
        run_and_wait(client, session="amp", timeout=60)
    assert isinstance(caught.value, writer.MaestroCompletionUnknown)
    metadata = caught.value.result.metadata
    assert metadata["outcome"] == "unknown"
    assert metadata["history"] == "Interactive.11"
    assert metadata["session"] == "amp"
    assert metadata["completion_marker"] == "/tmp/vb_sim_done_deadbeef"
    assert metadata["simulation_start_sent"] is True


def test_wait_until_done_caps_remote_poll_and_sleep_to_budget(monkeypatch) -> None:
    clock = [100.0]
    command_timeouts: list[float] = []
    sleeps: list[float] = []

    class _Runner:
        def run_command(self, _command, *, timeout):
            command_timeouts.append(timeout)
            return SimpleNamespace(returncode=1, stdout="")

    client = SimpleNamespace(ssh_runner=_Runner())

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", fake_sleep)

    with pytest.raises(TimeoutError, match=r"within 3s"):
        writer._wait_until_done(client, "/tmp/missing-marker", timeout=3)

    assert command_timeouts == [3, 1]
    assert sleeps == [2, 1]


def test_create_netlist_for_corner_passes_explicit_session() -> None:
    client = _RecordingClient()

    create_netlist_for_corner(
        client,
        "tran_test",
        "tt",
        "/tmp/tran_tt",
        session="session3",
    )

    assert client.expressions == [
        'maeCreateNetlistForCorner("tran_test" "tt" "/tmp/tran_tt" '
        '?session "session3")'
    ]


def test_create_netlist_for_corner_session_is_keyword_only() -> None:
    client = _RecordingClient()

    with pytest.raises(TypeError):
        create_netlist_for_corner(
            client,
            "tran_test",
            "tt",
            "/tmp/tran_tt",
            "session3",
        )


def test_maestro_ops_passes_explicit_session_to_corner_netlist_export() -> None:
    client = _RecordingClient()

    MaestroOps(client).create_netlist_for_corner(
        "tran_test",
        "tt",
        "/tmp/tran_tt",
        session="session3",
    )

    assert client.expressions == [
        'maeCreateNetlistForCorner("tran_test" "tt" "/tmp/tran_tt" '
        '?session "session3")'
    ]


def test_run_turns_off_the_history_name_form_for_that_call_only() -> None:
    # A site setting can open a modal "Specify History Name" form before
    # every run; nothing answers it for a programmatic run.
    skill = _without_history_name_form("maeRunSimulation()")
    tool_var = '"adexl.historyNamePrefix" "showNameHistoryForm"'

    probe = f"isCallable('envIsVal) && envIsVal({tool_var} t) && envGetVal({tool_var})"
    off = f"when(vbFormOn envSetVal({tool_var} 'boolean nil))"
    run = "vbRun = errset(maeRunSimulation() t)"
    restore = f"when(vbFormOn envSetVal({tool_var} 'boolean t))"
    assert probe in skill
    # Restored after the call whether or not it failed, before any error.
    assert skill.index(off) < skill.index(run) < skill.index(restore) < skill.index("error(")
    assert skill.endswith("car(vbRun))")


@pytest.mark.parametrize("recovery", [False, True])
def test_start_error_keeps_marker_and_original_recovery_error(monkeypatch, recovery):
    client = _RecordingClient()
    result = VirtuosoResult(status=ExecutionStatus.ERROR, errors=["socket error"],
                            metadata={"request_sent": None, "outcome": "unknown",
                                      "dialog_guard": {"status": "blocked"}})
    if recovery:
        result.metadata["request_handle"] = RequestHandle(
            request_id="a" * 32, daemon_instance="b" * 32, virtuoso_pid=42).model_dump()
        original = RequestRecoveryError(result)
    else:
        original = DialogBlockedError(result)
    starts = []
    def start(*a, **kw):
        starts.append(kw)
        raise original
    monkeypatch.setattr(writer, "run_simulation", start)
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: None)
    with pytest.raises(type(original)) as caught:
        run_and_wait(client, session="session1", timeout=30)
    assert caught.value is original
    assert len(starts) == 1
    assert result.metadata["completion_marker"].startswith("/tmp/vb_sim_done_")
    assert result.metadata["session"] == "session1"
    assert result.metadata["simulation_start_sent"] is None


@pytest.mark.parametrize("status", ["clear", "blocked", "indeterminate"])
def test_start_transport_error_inspects_only_enabled_guard(monkeypatch, status):
    original = ConnectionError("socket reset")
    calls = []
    def annotate(result, timeout):
        calls.append(timeout)
        result.metadata["dialog_guard"] = {"status": status}
        return result
    client = _RecordingClient()
    client.dialogs = SimpleNamespace(enabled=True, annotate_failure=annotate)
    def failed(*a, **kw):
        raise original
    monkeypatch.setattr(client, "execute_skill", failed)
    expected = DialogBlockedError if status == "blocked" else ConnectionError
    with pytest.raises(expected) as caught:
        run_simulation(client, session="amp", timeout=30)
    if status != "blocked":
        assert caught.value is original
    assert len(calls) == 1 and 0 < calls[0] <= 5
    assert caught.value.result.metadata["simulation_start_sent"] is None
    assert caught.value.result.metadata["session"] == "amp"


def test_completion_transport_error_carries_dialog_and_acknowledged_run(monkeypatch):
    client = _RecordingClient()
    def annotate(result, timeout):
        assert 0 < timeout <= 5
        result.metadata["dialog_guard"] = {"status": "blocked"}
        return result
    client.dialogs = SimpleNamespace(enabled=True, annotate_failure=annotate)
    monkeypatch.setattr(writer, "run_simulation", lambda *a, **k: '"Interactive.17"')
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: None)
    def poll(*a, **kw):
        raise ConnectionError("poll transport lost")
    monkeypatch.setattr(writer, "_wait_until_done", poll)
    with pytest.raises(DialogBlockedError) as caught:
        run_and_wait(client, session="amp", timeout=30)
    metadata = caught.value.result.metadata
    assert metadata["history"] == "Interactive.17"
    assert metadata["simulation_start_sent"] is True
    assert metadata["phase"] == "completion_wait"
    assert metadata["outcome"] == "unknown"


def test_start_guard_refusal_preserves_not_started_and_does_not_inspect_twice(monkeypatch):
    client = _RecordingClient()
    client.dialogs = SimpleNamespace(enabled=True, annotate_failure=lambda *a, **kw:
                                    pytest.fail("existing evidence must be preserved"))
    result = VirtuosoResult(status=ExecutionStatus.ERROR, errors=["blocked"], metadata={
        "dialog_guard": {"status": "blocked"}, "request_sent": False, "outcome": "not_started"})
    monkeypatch.setattr(client, "execute_skill", lambda *a, **kw: result)
    with pytest.raises(DialogBlockedError) as caught:
        run_and_wait(client, session="amp", timeout=30)
    assert caught.value.result.metadata["simulation_start_sent"] is False
    assert caught.value.result.metadata["outcome"] == "not_started"


def test_failed_diagnostic_inspection_does_not_mask_or_repeat_start_error(monkeypatch):
    client = _RecordingClient()
    inspections = []
    def annotate(*a, **kw):
        inspections.append(kw)
        raise ConnectionError("inspection unavailable")
    client.dialogs = SimpleNamespace(enabled=True, annotate_failure=annotate)
    original = ConnectionError("start socket reset")
    def execute(expression, **kw):
        if "maeRunSimulation" in expression:
            raise original
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")
    monkeypatch.setattr(client, "execute_skill", execute)
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: None)
    with pytest.raises(ConnectionError) as caught:
        run_and_wait(client, session="amp", timeout=30)
    assert caught.value is original
    assert len(inspections) == 1
    assert original.result.metadata["dialog_inspection_error"] == "inspection unavailable"
    assert original.result.metadata["completion_marker"].startswith("/tmp/vb_sim_done_")


def test_new_inspection_updates_existing_recovery_exception_fields(monkeypatch):
    client = _RecordingClient()
    result = VirtuosoResult(status=ExecutionStatus.ERROR, errors=["socket reset"], metadata={
        "request_handle": RequestHandle(request_id="a" * 32, daemon_instance="b" * 32,
                                        virtuoso_pid=42).model_dump(),
        "request_sent": None, "outcome": "unknown"})
    original = RequestRecoveryError(result)
    handle = original.handle
    def annotate(result, timeout):
        result.metadata["dialog_guard"] = {"status": "blocked"}
    client.dialogs = SimpleNamespace(enabled=True, annotate_failure=annotate)
    def failed(*a, **kw):
        raise original
    monkeypatch.setattr(client, "execute_skill", failed)
    with pytest.raises(RequestRecoveryError) as caught:
        run_simulation(client, timeout=30)
    assert caught.value is original
    assert original.handle == handle
    assert original.inspection == {"status": "blocked"}
    assert original.outcome == original.result.metadata["outcome"] == "unknown"


def test_budget_exhausted_before_start_is_definitely_not_started(monkeypatch):
    client = _RecordingClient()
    clock = [0.0]
    def setup(*a, **kw):
        clock[0] = 31
        return "t"
    monkeypatch.setattr(writer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(writer, "_q", setup)
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: None)
    monkeypatch.setattr(writer, "run_simulation", lambda *a, **kw: pytest.fail("no budget to start"))
    with pytest.raises(TimeoutError) as caught:
        run_and_wait(client, session="amp", timeout=30)
    metadata = caught.value.result.metadata
    assert metadata["simulation_start_sent"] is False
    assert metadata["request_sent"] is False
    assert metadata["outcome"] == "not_started"

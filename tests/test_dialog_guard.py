from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from virtuoso_bridge import VirtuosoClient, cli, daemon_auth
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso import x11
from virtuoso_bridge.virtuoso.dialogs import DialogBlockedError, DialogInspection, DialogTarget
from virtuoso_bridge.virtuoso.maestro import writer


def _payload(status="clear", **overrides):
    result = {
        "status": status, "target": {"pid": 42, "display": ":7", "ciw_window": "0x10"},
        "dialogs": [], "diagnostics": [],
    }
    if status == "blocked":
        result["dialogs"] = [{"window_id": "0x20", "title": "User Save Changes", "source": "user"}]
    result.update(overrides)
    return result


def _client(monkeypatch, payload=None):
    client = VirtuosoClient(daemon_token="a" * 64)
    client._tunnel = SimpleNamespace(gui_host="gui", daemon_host="gui", gui_runner=SimpleNamespace(user="tester"))
    client._daemon_caps = {"auth": "on", "virtuoso_pid": 42}
    monkeypatch.setattr(client, "_ensure_daemon_capabilities", lambda deadline, **kw: client._daemon_caps)
    monkeypatch.setattr(x11, "inspect_dialogs", lambda *args, **kw: _payload() if payload is None else payload)
    return client


@pytest.mark.parametrize("pid", [0, -1, True, "42"])
def test_target_rejects_invalid_pid(pid):
    with pytest.raises(ValueError):
        DialogTarget(pid=pid)


@pytest.mark.parametrize("field,value", [
    ("display", "--dismiss"), ("display", ":7\n"),
    ("ciw_window", "0x10;rm"), ("ciw_window", "0"),
])
def test_target_rejects_invalid_identifiers(field, value):
    with pytest.raises(ValueError):
        DialogTarget(pid=42, **{field: value})


def test_guard_binding_uses_capabilities_not_skill(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "execute_skill", lambda *a, **k: pytest.fail("must not probe SKILL"))
    report = client.dialogs.enable_guard()
    assert report.status == "clear"
    assert client.dialogs.target == DialogTarget(pid=42, display=":7", ciw_window="0x10")


def test_binding_and_preflight_refresh_signed_identity_without_skill(monkeypatch):
    token = "a" * 64
    client = VirtuosoClient(daemon_token=token)
    client._daemon_caps = {"auth": "on", "virtuoso_pid": 999}
    requests = []
    def exchange(payload, deadline):
        requests.append(payload)
        body = json.dumps({"proto": daemon_auth.PROTOCOL_VERSION, "auth": "on", "virtuoso_pid": 42})
        mac = daemon_auth.response_mac(token, nonce=payload["nonce"], marker="\x02", body=body)
        return ("\x02" + mac + body).encode()
    monkeypatch.setattr(client, "_exchange_payload", exchange)
    monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: _payload())
    monkeypatch.setattr(client, "_execute_skill_unguarded", lambda *a, **k: VirtuosoResult(status=ExecutionStatus.SUCCESS))
    assert client.dialogs.enable_guard(local_gui=True).target.pid == 42
    assert client.execute_skill("save()").ok
    assert len(requests) == 2
    assert all(item["op"] == "hello" and "skill" not in item for item in requests)


def test_loopback_alone_does_not_prove_local_gui(monkeypatch):
    client = VirtuosoClient(host="127.0.0.1", daemon_token="a" * 64)
    monkeypatch.setattr(client, "_ensure_daemon_capabilities", lambda *a, **k: pytest.fail("no known GUI transport"))
    with pytest.raises(ValueError, match="local_gui=True"):
        client.dialogs.enable_guard()
    assert not client.dialogs.enabled


def test_initial_user_dialog_enables_protection_without_dismissal(monkeypatch):
    client = _client(monkeypatch, _payload("blocked"))
    report = client.dialogs.enable_guard()
    assert report.dialogs[0]["source"] == "unknown"
    assert report.dialogs[0]["suggested_action"] is None
    assert client.dialogs.enabled


@pytest.mark.parametrize("caps", [
    {"auth": "off", "virtuoso_pid": 42}, {"auth": "on"},
    {"auth": "on", "virtuoso_pid": True},
])
def test_guard_refuses_unverifiable_daemon(monkeypatch, caps):
    client = _client(monkeypatch)
    client._daemon_caps = caps
    with pytest.raises(ValueError):
        client.dialogs.enable_guard()
    assert not client.dialogs.enabled


def test_guard_refuses_wrong_pid_and_split_host(monkeypatch):
    client = _client(monkeypatch)
    with pytest.raises(ValueError, match="does not match"):
        client.dialogs.enable_guard(pid=43)
    client._tunnel = SimpleNamespace(gui_host="gui", daemon_host="compute")
    with pytest.raises(ValueError, match="same host"):
        client.dialogs.enable_guard()


@pytest.mark.parametrize("payload", [
    _payload("blocked"), _payload("indeterminate"),
    _payload(target={"pid": 43, "display": ":7", "ciw_window": "0x10"}),
    _payload(dialogs=[{"title": "contradictory clear"}]),
    _payload(target={"pid": 42, "display": None, "ciw_window": None}),
    {"error": "no X server"},
])
def test_preexisting_or_unknown_dialog_never_sends_request(monkeypatch, payload):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: payload)
    monkeypatch.setattr(client, "_execute_skill_unguarded", lambda *a, **k: pytest.fail("must not send"))
    result = client.execute_skill("dbSave(cv)")
    assert not result.ok
    assert result.metadata["request_sent"] is False
    assert result.metadata["outcome"] == "not_started"


def test_inspection_transport_failure_is_indeterminate(monkeypatch):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    def failed(*a, **k):
        raise TimeoutError("X11 unavailable")
    monkeypatch.setattr(x11, "inspect_dialogs", failed)
    result = client.execute_skill("1+2")
    assert result.metadata["dialog_guard"]["status"] == "indeterminate"
    assert result.metadata["request_sent"] is False


def test_changed_endpoint_cannot_reuse_old_guard_identity(monkeypatch):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    client._port += 1
    monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: pytest.fail("old target"))
    monkeypatch.setattr(client, "_execute_skill_unguarded", lambda *a, **k: pytest.fail("new endpoint"))
    result = client.execute_skill("save()")
    assert result.metadata["request_sent"] is False
    assert "endpoint changed" in result.metadata["dialog_guard"]["diagnostics"][0]


def test_redirected_same_port_daemon_is_refused_before_skill(monkeypatch):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    client._daemon_caps = {"auth": "on", "virtuoso_pid": 43}
    monkeypatch.setattr(client, "_execute_skill_unguarded", lambda *a, **k: pytest.fail("wrong CIW"))
    result = client.execute_skill("save()")
    assert result.metadata["request_sent"] is False
    assert "identity" in result.metadata["dialog_guard"]["diagnostics"][0]


def test_clear_guard_executes_once_and_keeps_existing_return(monkeypatch):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    calls = []
    def execute(expression, timeout, **kwargs):
        calls.append((expression, timeout, kwargs))
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="3")
    monkeypatch.setattr(client, "_execute_skill_unguarded", execute)
    result = client.execute_skill("1+2", timeout=30, retry_connect=False)
    assert result.ok and result.output == "3"
    assert len(calls) == 1 and calls[0][0] == "1+2"
    assert 0 < calls[0][1] <= 30 and calls[0][2] == {"retry_connect": False}


def test_dialog_during_request_is_unknown_and_never_replayed(monkeypatch):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    calls = []
    def execute(*a, **k):
        calls.append(a)
        assert k["retry_connect"] is False
        monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: _payload("blocked"))
        return VirtuosoResult(status=ExecutionStatus.ERROR, errors=["SKILL execution timeout"])
    monkeypatch.setattr(client, "_execute_skill_unguarded", execute)
    result = client.execute_skill("maeRunSimulation()")
    assert len(calls) == 1
    assert result.metadata["outcome"] == "unknown"
    assert result.metadata["dialog_guard"]["status"] == "blocked"
    assert result.warnings
    # Once the human closes the dialog, the next explicit request is allowed.
    monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: _payload())
    monkeypatch.setattr(client, "_execute_skill_unguarded", lambda *a, **k: VirtuosoResult(status=ExecutionStatus.SUCCESS))
    assert client.execute_skill("readback()").ok


def test_exhausted_budget_does_not_trigger_another_inspection(monkeypatch):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    clock = [0.0]
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.time.monotonic", lambda: clock[0])
    def execute(*a, **k):
        clock[0] = 30
        monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: pytest.fail("no budget"))
        return VirtuosoResult(status=ExecutionStatus.ERROR, errors=["timeout"])
    monkeypatch.setattr(client, "_execute_skill_unguarded", execute)
    result = client.execute_skill("save()", timeout=30)
    assert result.metadata["dialog_guard"]["status"] == "indeterminate"
    assert result.metadata["outcome"] == "unknown"


def test_guard_disabled_keeps_legacy_execution_path(monkeypatch):
    client = _client(monkeypatch)
    expected = VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")
    monkeypatch.setattr(client, "_execute_skill_unguarded", lambda *a, **k: expected)
    assert client.execute_skill("1+2") is expected
    client.dialogs.enable_guard()
    client.dialogs.disable_guard()
    assert client.execute_skill("1+2") is expected


def test_bulk_dismissal_requires_optin_and_guard_refuses_it(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(x11, "dismiss_dialogs", lambda *a, **k: pytest.fail("no injection"))
    assert "error" in client.dismiss_dialog()[0]
    client.dialogs.enable_guard()
    assert "error" in client.dismiss_dialog(allow_legacy_bulk=True)[0]


def test_high_level_maestro_retains_blocker_evidence(monkeypatch):
    client = _client(monkeypatch, _payload("blocked"))
    client.dialogs.enable_guard()
    with pytest.raises(DialogBlockedError) as caught:
        writer.run_simulation(client, session="s1")
    assert caught.value.outcome == "not_started"
    assert caught.value.inspection["status"] == "blocked"


def test_maestro_nil_does_not_close_forms_probe_or_retry(monkeypatch):
    calls = []
    class Client:
        ssh_runner = None
        def execute_skill(self, expression, **kwargs):
            calls.append(expression)
            return SimpleNamespace(output="nil" if "maeRunSimulation(" in expression else "t", errors=[])
        def dismiss_dialog(self):
            pytest.fail("must not dismiss user dialog")
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: None)
    monkeypatch.setattr(writer, "_wait_until_done", lambda *a, **k: pytest.fail("no run acknowledged"))
    with pytest.raises(RuntimeError, match="not retried"):
        writer.run_and_wait(Client(), session="s1")
    assert len(calls) == 2
    assert sum("maeRunSimulation(" in x for x in calls) == 1
    assert all("hiFormDone" not in x and "hiGetCurrentForm" not in x for x in calls)


def test_guarded_maestro_nil_reports_new_dialog_and_completion_evidence(monkeypatch):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    calls = []
    def execute(expression, *a, **kw):
        calls.append(expression)
        if "maeRunSimulation(" in expression:
            monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: _payload("blocked"))
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="nil")
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")
    monkeypatch.setattr(client, "_execute_skill_unguarded", execute)
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: None)
    with pytest.raises(DialogBlockedError) as caught:
        writer.run_and_wait(client, session="s1")
    assert len(calls) == 2
    assert caught.value.inspection["status"] == "blocked"
    assert caught.value.outcome == "unknown"
    assert caught.value.result.metadata["completion_marker"].startswith("/tmp/vb_sim_done_")


def test_user_dialog_during_completion_wait_does_not_cancel_run(monkeypatch):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: _payload("blocked"))
    removed = []
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: removed.append(a))
    monkeypatch.setattr(client, "execute_skill", lambda *a, **k: pytest.fail("no SKILL during wait"))
    with pytest.raises(DialogBlockedError) as caught:
        writer._wait_until_done(client, "/tmp/vb-missing-marker-test", timeout=600)
    assert caught.value.outcome == "unknown"
    assert caught.value.result.metadata["phase"] == "completion_wait"
    assert "request_sent" not in caught.value.result.metadata
    assert not removed


def test_completion_wait_checks_are_low_frequency_and_keep_history(monkeypatch):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    clock = [100.0]
    inspections = []
    def inspect(*a, **kw):
        inspections.append((clock[0], kw["timeout"]))
        return _payload() if len(inspections) == 1 else _payload("blocked")
    monkeypatch.setattr(x11, "inspect_dialogs", inspect)
    monkeypatch.setattr(writer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(writer.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: None)
    monkeypatch.setattr(writer, "_q", lambda *a, **k: "t")
    monkeypatch.setattr(writer, "run_simulation", lambda *a, **k: '"Interactive.8"')
    with pytest.raises(DialogBlockedError) as caught:
        writer.run_and_wait(client, session="s1", timeout=600)
    assert inspections == [(100.0, 5), (110.0, 5)]
    assert caught.value.result.metadata["history"] == "Interactive.8"
    assert caught.value.result.metadata["session"] == "s1"


@pytest.mark.parametrize("status,expected", [("clear", 0), ("blocked", 2), ("indeterminate", 1)])
def test_cli_inspects_without_skill_client(monkeypatch, capsys, status, expected):
    monkeypatch.setattr(cli, "load_vb_env", lambda: None)
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (None, "local"))
    monkeypatch.setattr(cli, "_get_cli_profile", lambda: "sos")
    monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: _payload(status))
    rc = cli.cli_inspect_dialogs(pid=42, json_output=True)
    assert rc == expected
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_cli_default_dismissal_does_not_connect(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: pytest.fail("must not connect"))
    assert cli.cli_dismiss_dialog() == 1
    assert "disabled" in capsys.readouterr().out


@pytest.mark.parametrize("stage,error", [
    ("load_vb_env", ValueError("invalid configuration")),
    ("_make_ssh_runner", SystemExit("GUI host not configured")),
])
def test_cli_inspection_setup_failure_is_json(monkeypatch, capsys, stage, error):
    monkeypatch.setattr(cli, "load_vb_env", lambda: None)
    def fail():
        raise error
    monkeypatch.setattr(cli, stage, fail)
    assert cli.cli_inspect_dialogs(pid=42, json_output=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "indeterminate"
    assert report["diagnostics"] == [str(error)]


def test_cli_explicit_dismissal_preserves_display(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (None, "local"))
    seen = []
    def dismiss(*args, **kwargs):
        seen.append(kwargs)
        return [{"dismissed": "0x10", "action": "escape"}]
    monkeypatch.setattr(x11, "dismiss_window", dismiss)
    assert cli.cli_dismiss_window(window_id="0x10", action="escape", display=":7") == 0
    assert seen[0]["display"] == ":7"
    assert seen[0]["action"] == "escape"


def test_explicit_dismissal_quotes_display(monkeypatch):
    commands = []
    monkeypatch.setattr(x11, "load_vb_env", lambda: None)
    monkeypatch.setattr(x11, "_ensure_helper", lambda *a: "/helper.py")
    monkeypatch.setattr(x11, "_detect_remote_python", lambda *a: "python3")
    def run(runner, command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="{}\n", stderr="")
    monkeypatch.setattr(x11, "_run", run)
    x11.dismiss_window(None, "local", "0x10", display=":7; echo unsafe")
    assert commands[0].endswith(" ':7; echo unsafe'")


def test_x11_inspection_uses_quoted_content_addressed_helper_and_cache(monkeypatch):
    commands = []
    uploads = []
    class Runner:
        def run_command(self, command, timeout=None):
            commands.append((command, timeout))
            if "--inspect-dialogs" in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps(_payload()), stderr="")
            return SimpleNamespace(returncode=0, stdout="CMD:python3\n", stderr="")
        def upload(self, path, remote_path, timeout=None):
            uploads.append(remote_path)
            return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(x11, "default_virtuoso_bridge_dir", lambda *a: "/shared root/x11")
    monkeypatch.setattr(x11, "resolve_client_id", lambda *a: "test")
    runner = Runner()
    for _ in range(2):
        assert x11.inspect_dialogs(runner, "user", pid=42, display=":7", ciw_window="0x10")["status"] == "clear"
    assert len(uploads) == 1
    assert "dialog_inspect_" in uploads[0]
    assert sum("CMD:python3" in cmd for cmd, _ in commands) == 1
    assert all(0 < timeout <= 15 for _, timeout in commands)
    assert "'/shared root/x11/dialog_inspect_" in commands[-1][0]


@pytest.mark.parametrize("stdout,returncode", [("", 0), ("not json", 0), (json.dumps(_payload()), 124)])
def test_x11_command_failure_never_becomes_clear(monkeypatch, stdout, returncode):
    monkeypatch.setattr(x11, "_detect_remote_python", lambda *a, **k: "python3")
    monkeypatch.setattr(x11, "_run", lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout, stderr="failed"))
    report = x11.inspect_dialogs(None, "user", pid=42)
    assert report["status"] == "indeterminate"


def test_low_level_bulk_default_does_not_run(monkeypatch):
    monkeypatch.setattr(x11, "_run", lambda *a, **k: pytest.fail("no injection"))
    assert "error" in x11.dismiss_dialogs(None, "user")[0]


def test_preflight_runs_the_inspection_and_the_identity_handshake_side_by_side(monkeypatch):
    # Sequentially they cost the sum of an SSH and a tunnel round trip.
    import threading

    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    inspecting, handshaking = threading.Event(), threading.Event()

    def inspect(*args, **kwargs):
        inspecting.set()
        assert handshaking.wait(2), "handshake did not run during the inspection"
        return _payload()

    def handshake(deadline, **kwargs):
        handshaking.set()
        assert inspecting.wait(2), "inspection did not run during the handshake"
        return client._daemon_caps

    monkeypatch.setattr(x11, "inspect_dialogs", inspect)
    monkeypatch.setattr(client, "_ensure_daemon_capabilities", handshake)
    assert client.dialogs.preflight(timeout=5) is None


@pytest.mark.parametrize("inspection, caps, expected", [
    ("blocked", {"auth": "on", "virtuoso_pid": 42}, "blocked"),
    ("clear", {"auth": "on", "virtuoso_pid": 7}, "indeterminate"),
    ("clear", {"auth": "off", "virtuoso_pid": 42}, "indeterminate"),
])
def test_parallel_preflight_still_needs_both_checks_to_pass(monkeypatch, inspection, caps, expected):
    client = _client(monkeypatch)
    client.dialogs.enable_guard()
    monkeypatch.setattr(x11, "inspect_dialogs", lambda *a, **k: _payload(inspection))
    monkeypatch.setattr(client, "_ensure_daemon_capabilities", lambda deadline, **k: caps)
    monkeypatch.setattr(client, "_execute_skill_unguarded", lambda *a, **k: pytest.fail("SKILL was sent"))

    result = client.execute_skill("1+2", timeout=5)

    assert result.metadata["request_sent"] is False
    assert result.metadata["dialog_guard"]["status"] == expected

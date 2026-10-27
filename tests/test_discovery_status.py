"""Status must reconcile live records before probing a saved tunnel."""
import json

import pytest

from virtuoso_bridge import cli
from virtuoso_bridge.transport.tunnel import SSHClient
from tests.test_instance_discovery import row


@pytest.fixture
def status(monkeypatch):
    client = SSHClient(remote_host="fixture", discover_instances=True)
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_CLI_PROFILE", [None])
    monkeypatch.setattr(cli, "_print_spectre_status", lambda *a: None)
    monkeypatch.setattr(SSHClient, "from_env", lambda **kw: client)
    state = dict(port=65082, remote_port=None)
    monkeypatch.setattr(SSHClient, "read_state", lambda *a: state)
    monkeypatch.setattr(SSHClient, "is_running", lambda *a: True)
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(client, "refresh_daemon_token", lambda: pytest.fail("stale endpoint probed"))
    return client, state


def test_two_live_instances_replace_stale_tunnel_no_response(status, monkeypatch, capsys):
    client, _ = status
    monkeypatch.setattr(client, "list_instances", lambda: [row(), row(65002, 20, "/project/b")])
    assert cli._print_status() == 1
    out = capsys.readouterr().out
    assert "65001  10  /project/a" in out and "65002  20  /project/b" in out
    assert "--port PORT" in out
    assert "NO RESPONSE" not in out and "load(" not in out


@pytest.mark.parametrize("saved", [None, row(pid=99)])
def test_available_instance_is_not_a_connection(status, monkeypatch, capsys, saved):
    client, _ = status
    client.instance_record = saved
    monkeypatch.setattr(client, "list_instances", lambda: [row()])
    assert cli._print_status() == 1
    assert "AVAILABLE" in capsys.readouterr().out


def test_status_respects_directory_and_port(status, monkeypatch, capsys):
    client, _ = status
    client._expected_work_dir = "/project/b"
    client._selected_port = 65001
    monkeypatch.setattr(client, "list_instances", lambda: [row(), row(65002, 20, "/project/b")])
    assert cli._print_status() == 1
    assert "No matching live instance" in capsys.readouterr().out


@pytest.mark.parametrize("wrong_identity", [False, True])
def test_status_authenticates_exact_saved_record_without_skill(status, monkeypatch, capsys, wrong_identity):
    from tests.test_daemon_auth import _AuthDaemon, TOKEN
    client, state = status
    selected = row()
    client.instance_record = selected
    monkeypatch.setattr(client, "list_instances", lambda: [selected, row(65002, 20)])
    monkeypatch.setattr(client, "refresh_daemon_token", lambda: TOKEN)
    # The old cross-user override must not bypass signed record matching.
    monkeypatch.setenv("VB_ALLOW_CROSS_USER_DAEMON", "1")
    caps = dict(proto=1, auth="on", daemon="ramic-bridge", virtuoso_pid=10,
                instance_record=row(pid=99) if wrong_identity else selected)
    daemon = _AuthDaemon(caps_body=json.dumps(caps))
    try:
        state.update(port=daemon.port, remote_port=selected["port"])
        assert cli._print_status() == int(wrong_identity)
        out = capsys.readouterr().out
        assert ("does not match" if wrong_identity else "OK - authenticated") in out
        assert not daemon.executed
    finally:
        daemon.close()

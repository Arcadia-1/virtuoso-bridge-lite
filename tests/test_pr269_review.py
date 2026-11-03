"""Behavioral regressions for PR #269's compatibility and selection review."""
import json
import os

import pytest

from virtuoso_bridge import env
from virtuoso_bridge.instance_discovery import InstanceSelectionError
from virtuoso_bridge.transport import tunnel
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient, _default_remote_port
from tests.test_instance_discovery import row


@pytest.fixture
def configuration(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("VB_") and key != "VB_STATE_DIR":
            monkeypatch.delenv(key)
    path = tmp_path / "empty.env"
    path.write_text("")
    monkeypatch.setattr(env, "_RUNTIME_ENV_FILE", path)
    monkeypatch.setattr(tunnel, "resolve_profile", lambda profile: profile)
    monkeypatch.setenv("VB_REMOTE_HOST", "fixture")
    monkeypatch.setenv("VB_REMOTE_USER", "fixture")


@pytest.mark.parametrize("profile", [None, "project"])
@pytest.mark.parametrize("automatic", [None, "0", "1"])
@pytest.mark.parametrize("port", [None, "61000"])
def test_mode_and_endpoint_are_explicit(configuration, monkeypatch, profile, automatic, port):
    suffix = "_" + profile if profile else ""
    monkeypatch.setenv("VB_REMOTE_HOST" + suffix, "fixture")
    monkeypatch.setenv("VB_REMOTE_USER" + suffix, "fixture")
    monkeypatch.setenv("VB_REMOTE_WORK_DIR" + suffix, "/project/a")
    if profile:
        monkeypatch.setenv("VB_AUTO_SESSIONS", "1")  # never leak to a profile
    if automatic is not None:
        monkeypatch.setenv("VB_AUTO_SESSIONS" + suffix, automatic)
    if port:
        monkeypatch.setenv("VB_REMOTE_PORT" + suffix, port)
    calls = []
    monkeypatch.setattr(tunnel, "_read_selection", lambda *a: calls.append("selection") or {})
    client = tunnel.SSHClient.from_env(profile=profile)
    assert client._discover_instances is (automatic == "1")
    assert client.remote_port == (int(port) if port else _default_remote_port("fixture"))
    assert client.expected_work_dir == ("/project/a" if automatic == "1" else "")
    assert calls == (["selection"] if automatic == "1" else [])


def test_legacy_workdir_only_reuses_default_port_state(configuration, monkeypatch):
    monkeypatch.setenv("VB_REMOTE_WORK_DIR", "/project/a")
    client = tunnel.SSHClient.from_env()
    state = dict(mode="remote", port=client.port, remote_port=client.remote_port,
                 remote_host="fixture", remote_user="fixture", tunnel_pid=123)
    tunnel._write_private_json(tunnel.legacy_cache_state_file(), state)
    monkeypatch.setattr(tunnel, "_read_selection", lambda *a: pytest.fail("legacy read selection"))
    assert tunnel.SSHClient.read_state() == state
    bridge = VirtuosoClient.from_env()
    assert bridge._tunnel.remote_port == _default_remote_port("fixture")
    assert not bridge._tunnel._discover_instances


@pytest.mark.parametrize("entrypoint", ["discover", "warm", "python"])
@pytest.mark.parametrize("directory", ["", "/project/a"])
def test_vanished_saved_identity_never_retargets(configuration, monkeypatch, entrypoint, directory):
    client = tunnel.SSHClient("fixture", discover_instances=True, expected_work_dir=directory)
    original = row(61000, 10, "/project/a")
    other = row(62000, 20, "/project/b" if not directory else "/project/a")
    client.instance_record = original
    monkeypatch.setattr(client, "list_instances", lambda **kw: [other])
    for method in ("ensure_tunnel", "refresh_daemon_token", "ensure_remote_setup", "save_state"):
        monkeypatch.setattr(client, method, lambda *a, **kw: pytest.fail("retargeted or provisioned"))
    with pytest.raises(InstanceSelectionError, match="select explicitly"):
        if entrypoint == "discover":
            client.discover_instance(provision=False)
        elif entrypoint == "warm":
            client.warm()
        else:
            import time
            bridge = VirtuosoClient(tunnel=client)
            bridge._ensure_daemon_capabilities(time.monotonic() + 2)
    assert client.instance_record == original


def test_explicit_port_reselects_reused_endpoint(configuration, monkeypatch):
    monkeypatch.setenv("VB_AUTO_SESSIONS", "1")
    original, replacement = row(61000), row(61000, 20)
    monkeypatch.setattr(tunnel, "_read_selection", lambda *a: dict(record=original, port=61000))
    client = tunnel.SSHClient.from_env(selected_port=61000)
    assert client.instance_record == original  # status can still compare saved identity
    assert client.select_record([replacement]) == replacement


def test_legacy_status_does_not_read_registry(configuration, monkeypatch, capsys):
    from virtuoso_bridge import cli
    client = tunnel.SSHClient.from_env()
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_print_spectre_status", lambda *a: None)
    monkeypatch.setattr(tunnel.SSHClient, "from_env", lambda **kw: client)
    monkeypatch.setattr(client, "list_instances", lambda: pytest.fail("legacy status read registry"))
    monkeypatch.setattr(client, "read_daemon_token", lambda: "ab" * 32)
    monkeypatch.setattr(client, "read_daemon_identity", lambda: {})
    monkeypatch.setattr(client, "close", lambda: None)
    state = dict(mode="local", port=client.port)
    monkeypatch.setattr(tunnel.SSHClient, "read_state", lambda *a, **kw: state)
    monkeypatch.setattr(VirtuosoClient, "_ensure_daemon_capabilities", lambda *a: dict(auth="on", virtuoso_pid=10))
    assert cli._print_status() == 0
    assert "configured legacy endpoint" in capsys.readouterr().out


def test_fixture_redirects_both_historical_state_helpers(tmp_path):
    from virtuoso_bridge import runtime_paths
    expected = tmp_path / "legacy-state" / "state.json"
    assert runtime_paths.legacy_cache_state_file() == expected
    assert tunnel.legacy_cache_state_file() == expected
    tunnel._write_private_json(expected, dict(mode="local", port=61000))
    before = expected.read_bytes()
    assert tunnel.SSHClient.read_state(target=None)["port"] == 61000
    assert expected.read_bytes() == before


def test_default_restart_uses_legacy_path_without_registry(configuration, monkeypatch):
    client = tunnel.SSHClient.from_env()
    monkeypatch.setattr(client, "list_instances", lambda **kw: pytest.fail("legacy restart read registry"))
    calls = []
    monkeypatch.setattr(client, "_restart_legacy_instance", lambda timeout: calls.append(timeout))
    client.restart_instance(timeout=5)
    assert calls == [5]

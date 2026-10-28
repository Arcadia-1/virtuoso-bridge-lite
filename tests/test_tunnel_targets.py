"""Tunnels of different .env files stay separate without a profile.

Two configurations for different hosts, users or daemon ports used to share
one state file per profile: starting one replaced the other's record,
``stop`` stopped the wrong tunnel and clients connected to the wrong port
(authentication refused them). Each target now keeps its own state.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from virtuoso_bridge.transport.tunnel import SSHClient


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    from virtuoso_bridge.env import get_runtime_env_file, set_runtime_env_file

    # An empty selected .env: nothing outside the test can change VB_*.
    previous = get_runtime_env_file()
    (tmp_path / "empty.env").write_text("", encoding="utf-8")
    set_runtime_env_file(tmp_path / "empty.env")
    monkeypatch.setenv("VB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        "virtuoso_bridge.transport.tunnel.legacy_cache_state_file",
        lambda profile=None: tmp_path / "legacy-cache" / f"state{'_' + profile if profile else ''}.json",
    )
    monkeypatch.setattr("virtuoso_bridge.transport.tunnel._pid_is_alive", lambda pid: True)
    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHRunner.can_reach_port", lambda port: True)
    yield tmp_path / "state"
    set_runtime_env_file(previous)


def _configure(monkeypatch, host: str, user: str, port: int) -> None:
    """Load one .env's settings, as the CLI does before any command."""
    for key in [k for k in os.environ if k.startswith("VB_") and k != "VB_STATE_DIR"]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("VB_REMOTE_HOST", host)
    monkeypatch.setenv("VB_REMOTE_USER", user)
    monkeypatch.setenv("VB_REMOTE_PORT", str(port))


class _Runner:
    def __init__(self, pid: int) -> None:
        self.tunnel_pid = pid
        self.is_tunnel_alive = True
        self.stopped = False

    def stop_port_forward(self) -> None:
        self.stopped = True

    def close(self) -> None:
        pass


def _started(host: str, user: str, port: int, local_port: int, pid: int) -> SSHClient:
    client = SSHClient(remote_host=host, remote_user=user, port=port, local_port=local_port)
    client._ssh_runner = _Runner(pid)
    client._tunnel_endpoint = (local_port, port, pid)
    client.save_state()
    return client


A = ("eda-a", "alice", 65100)
B = ("eda-b", "bob", 65200)


def test_each_env_file_finds_only_its_own_tunnel(state_root, monkeypatch):
    _started(*A, local_port=65100, pid=111)
    _started(*B, local_port=65201, pid=222)

    _configure(monkeypatch, *A)
    assert SSHClient.read_state()["port"] == 65100 and SSHClient.is_running()
    _configure(monkeypatch, *B)
    assert SSHClient.read_state()["port"] == 65201 and SSHClient.is_running()
    # The same host with another daemon port is another tunnel.
    _configure(monkeypatch, "eda-a", "alice", 65101)
    assert SSHClient.read_state() is None and not SSHClient.is_running()

    assert sorted(target for _, target in SSHClient.running_targets()) == [
        "alice@eda-a:65100", "bob@eda-b:65200",
    ]


@pytest.mark.parametrize("other", [B, ("eda-a", "alice", 65101)])
def test_stopping_one_tunnel_leaves_the_other(state_root, monkeypatch, other):
    a = _started(*A, local_port=65100, pid=111)
    _started(*other, local_port=65201, pid=222)

    a.stop()

    assert a._ssh_runner.stopped
    _configure(monkeypatch, *A)
    assert SSHClient.read_state() is None
    _configure(monkeypatch, *other)
    assert SSHClient.read_state()["tunnel_pid"] == 222


def test_an_old_state_file_is_used_only_for_its_own_host(state_root, monkeypatch):
    from virtuoso_bridge.transport.tunnel import _write_private_json
    legacy = state_root / "state.json"
    _write_private_json(legacy, {
        "mode": "remote", "port": 65100, "remote_port": 65100,
        "daemon_host": "eda-a", "tunnel_pid": 111,
    })

    _configure(monkeypatch, *B)
    assert SSHClient.read_state() is None
    _configure(monkeypatch, *A)
    assert SSHClient.read_state()["tunnel_pid"] == 111

    # Saving the same target again moves it to its own file.
    _started(*A, local_port=65100, pid=111)
    assert not legacy.exists()
    assert SSHClient.read_state()["target"] == "alice@eda-a:65100"


def test_without_a_configuration_a_single_saved_tunnel_is_found(state_root, monkeypatch):
    _started(*A, local_port=65100, pid=111)
    for key in [k for k in os.environ if k.startswith("VB_") and k != "VB_STATE_DIR"]:
        monkeypatch.delenv(key)
    assert SSHClient.read_state()["tunnel_pid"] == 111

    _started(*B, local_port=65201, pid=222)
    assert SSHClient.read_state() is None


@pytest.mark.parametrize("remote_port", [None, "", 65085])
@pytest.mark.parametrize("legacy", [False, True])
def test_stale_state_cannot_drive_clients_or_stop(state_root, monkeypatch, remote_port, legacy):
    from virtuoso_bridge.transport import tunnel
    _configure(monkeypatch, *A)
    state_root.mkdir(parents=True)
    target = "alice@eda-a:65100"
    path = state_root / "state.json" if legacy else tunnel._state_file(None, target)
    path.write_text(json.dumps(dict(mode="remote", port=65082, remote_port=remote_port,
                                   target=target, daemon_host="eda-a", remote_user="alice",
                                   tunnel_pid=111)), encoding="utf-8")
    assert SSHClient.read_state() is None
    assert not SSHClient.is_running()
    assert SSHClient.running_targets() == []
    client = SSHClient.from_env(keep_remote_files=True)
    runner = SimpleNamespace(tunnel_pid=None, is_tunnel_alive=False,
                             stop_port_forward=lambda: None)
    client._ssh_runner = runner
    monkeypatch.setattr(client, "close", lambda: None)
    client.stop()
    assert runner.tunnel_pid is None  # the stale PID is never adopted or signalled


def test_auto_mode_requires_selection_even_if_old_fixed_port_is_live(state_root, monkeypatch):
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
    from virtuoso_bridge.daemon_auth import DaemonAuthError
    import time
    _configure(monkeypatch, *A)
    _started(*A, local_port=65082, pid=111)
    monkeypatch.delenv("VB_REMOTE_PORT")
    assert SSHClient.read_state() is None
    assert not SSHClient.is_running()
    client = VirtuosoClient.from_env()
    monkeypatch.setattr(client, "_exchange_payload", lambda *a: pytest.fail("stale listener contacted"))
    monkeypatch.setattr(client._tunnel, "list_instances", lambda **kw: [])
    from virtuoso_bridge.instance_discovery import InstanceSelectionError
    with pytest.raises(InstanceSelectionError, match="run virtuoso-bridge start"):
        client._ensure_daemon_capabilities(time.monotonic() + 1)


def test_saving_new_target_preserves_unidentified_legacy_state(state_root, monkeypatch):
    _configure(monkeypatch, *A)
    state_root.mkdir(parents=True)
    legacy = state_root / "state.json"
    old = json.dumps(dict(mode="remote", port=65082, daemon_host="eda-a", tunnel_pid=111))
    legacy.write_text(old, encoding="utf-8")
    _started(*A, local_port=65083, pid=222)
    assert SSHClient.read_state()["tunnel_pid"] == 222
    assert legacy.read_text(encoding="utf-8") == old

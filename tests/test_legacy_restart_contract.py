"""Legacy restart retains state and restarts only the already-loaded setup."""
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from virtuoso_bridge import daemon_auth, env
from virtuoso_bridge.transport import tunnel
from virtuoso_bridge.virtuoso.basic import bridge


@pytest.fixture
def legacy_restart(monkeypatch, tmp_path):
    config = tmp_path / "empty.env"
    config.write_text("")
    monkeypatch.setattr(env, "_RUNTIME_ENV_FILE", config)
    def make(remote=False, profile=None, outcome="success"):
        for key in list(os.environ):
            if key.startswith("VB_") and key != "VB_STATE_DIR":
                monkeypatch.delenv(key)
        suffix = "_" + profile if profile else ""
        monkeypatch.setenv("VB_REMOTE_HOST" + suffix, "fixture" if remote else "localhost")
        monkeypatch.setenv("VB_REMOTE_PORT" + suffix, "61000")
        monkeypatch.setattr(tunnel, "load_vb_env", lambda: None)
        monkeypatch.setattr(tunnel, "resolve_profile", lambda selected: selected)
        client = tunnel.SSHClient.from_env(profile=profile)
        if remote:
            client._ssh_runner._tunnel_pid = 12345
            client._local_port = 61000
        state = dict(mode="remote" if remote else "local", port=61000, remote_port=61000,
                     target=client.target, remote_host=client.daemon_host,
                     setup_path="/private/profile/virtuoso_setup.il",
                     identity_path="/private/profile/daemon_identity.txt", profile=profile,
                     tunnel_pid=12345 if remote else None)
        path = tunnel._state_file(profile, client.target)
        tunnel._write_private_json(path, state)
        assert client.read_state(profile, target=client.target) == state
        original = path.read_bytes()
        monkeypatch.setattr(client, "ensure_tunnel", lambda: None)
        monkeypatch.setattr(client, "read_daemon_token", lambda: "ab" * 32)
        monkeypatch.setattr(client, "_legacy_ciw_birth", lambda *a: ["boot", "birth"])
        for method in ("ensure_local_setup", "ensure_remote_setup", "list_instances"):
            monkeypatch.setattr(client, method, lambda *a, **kw: pytest.fail("restart deployed or discovered"))
        clock = [100.0]
        monkeypatch.setattr(tunnel.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(tunnel.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        requests = []

        class Peer:
            def __init__(self, **kwargs):
                pass

            def use_current_selection(self):
                pass

            def _ensure_daemon_capabilities(self, deadline, refresh=False):
                if refresh and outcome == "auth":
                    raise daemon_auth.DaemonAuthError("wrong peer")
                return dict(auth="on", virtuoso_pid=99 if refresh and outcome == "pid" else 10,
                            daemon_instance="new" if refresh and outcome != "timeout" else "old")

            def execute_skill(self, code, **kwargs):
                requests.append((code, kwargs))
                return SimpleNamespace(status="unknown", errors=["reply lost"])

        monkeypatch.setattr(bridge, "VirtuosoClient", Peer)
        return client, path, state, original, requests
    return make


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("profile", [None, "project"])
def test_from_env_restart_preserves_paths_after_uncertain_reply(legacy_restart, remote, profile):
    client, path, state, _, requests = legacy_restart(remote, profile)
    assert client.setup_path is None
    client.restart_instance(timeout=2)
    saved = tunnel.SSHClient.read_state(profile, target=client.target)
    assert saved["setup_path"] == state["setup_path"]
    assert saved["identity_path"] == state["identity_path"]
    assert len(requests) == 1 and requests[0][1]["retry_connect"] is False
    code = requests[0][0]
    assert "RBStop()" in code and "RBStart()" in code
    assert "load(" not in code and "setShellEnvVar(" not in code


@pytest.mark.parametrize("outcome", ["auth", "pid", "timeout"])
def test_unconfirmed_restart_preserves_original_state(legacy_restart, outcome):
    client, path, _, original, requests = legacy_restart(outcome=outcome)
    with pytest.raises((daemon_auth.DaemonAuthError, RuntimeError, TimeoutError)):
        client.restart_instance(timeout=2)
    assert path.read_bytes() == original
    assert len(requests) == 1 and requests[0][1]["retry_connect"] is False


def test_restart_never_restores_another_endpoint_paths(legacy_restart):
    client, path, state, _, _ = legacy_restart(remote=True)
    path.unlink()
    foreign = dict(state, target="fixture:62000", remote_port=62000,
                   setup_path="/other/setup.il", identity_path="/other/identity")
    tunnel._write_private_json(tunnel._state_file(target="fixture:62000"), foreign)
    client.restart_instance(timeout=2)
    saved = tunnel.SSHClient.read_state(target=client.target)
    assert saved["setup_path"] is None and saved["identity_path"] is None


def test_restart_preserves_already_restored_paths(legacy_restart):
    client, _, _, _, _ = legacy_restart()
    client._remote_virtuoso_setup_path = "/restored/setup.il"
    client._remote_identity_path = "/restored/identity"
    client.restart_instance(timeout=2)
    saved = tunnel.SSHClient.read_state(target=client.target)
    assert saved["setup_path"] == "/restored/setup.il"
    assert saved["identity_path"] == "/restored/identity"


def test_disabling_listen_all_requires_loading_the_prepared_setup(legacy_restart, monkeypatch):
    client, _, _, _, requests = legacy_restart()
    # The client config now requests loopback. Restart deliberately does not
    # send that config to the running CIW; only loading the generated setup does.
    monkeypatch.setenv("VB_DAEMON_LISTEN_ALL", "0")
    assert not client._listen_all
    client.restart_instance(timeout=2)
    assert "RB_LISTEN_ALL" not in requests[0][0]
    setup = tunnel._generate_virtuoso_setup_il(
        "/private/daemon.py", "/private/bridge.il", port=client.remote_port,
        listen_all=client._listen_all)
    assert 'setShellEnvVar("RB_LISTEN_ALL" "0")' in setup
    assert 'setShellEnvVar("RB_AUTO_PORT" "0")' in setup


def test_registered_cli_restart_help_explains_no_reload():
    entrypoint = Path(sys.executable).parent / ("virtuoso-bridge.exe" if os.name == "nt" else "virtuoso-bridge")
    result = subprocess.run([str(entrypoint), "restart", "--help"], capture_output=True,
                            text=True, timeout=15, check=True)
    assert "without deploying files or reloading setup" in " ".join(result.stdout.split())

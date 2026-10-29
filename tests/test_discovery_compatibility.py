"""Discovery-first compatibility, using local protocol peers and owned fixtures."""
import json
import os
import time
from types import SimpleNamespace

import pytest

from virtuoso_bridge import daemon_auth
from virtuoso_bridge.instance_discovery import DiscoveryError, InstanceSelectionError, format_instances
from virtuoso_bridge.transport import ssh, tunnel
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


def record(port=61001, pid=10):
    return dict(port=port, pid=pid, daemon_pid=11, uid=1000, user="fixture",
                work_dir="/project", instance="a" * 32)


@pytest.fixture
def legacy(monkeypatch, tmp_path):
    client = tunnel.SSHClient("localhost", discover_instances=True)
    client._legacy_port = 61000
    monkeypatch.setattr(client, "list_instances", lambda **kw: [])
    monkeypatch.setattr(client, "read_state", lambda *a, **kw: None)
    monkeypatch.setattr(client, "ensure_local_setup", lambda: pytest.fail("old setup must not be overwritten"))
    monkeypatch.setattr(client, "refresh_daemon_token", lambda: "ab" * 32)
    return client


def test_empty_discovery_uses_old_port_without_provisioning(legacy):
    legacy.discover_instance()
    assert legacy._legacy_fallback and legacy.remote_port == 61000
    assert legacy.instance_record is None


def test_new_session_is_adopted_with_unchanged_old_config(legacy, monkeypatch):
    legacy.discover_instance()
    new = record()
    monkeypatch.setattr(legacy, "list_instances", lambda **kw: [new])
    legacy.discover_instance()
    assert not legacy._legacy_fallback
    assert legacy.instance_record == new and legacy.remote_port == new["port"]


def test_other_project_records_do_not_prevent_old_endpoint_use(legacy, monkeypatch):
    legacy._expected_work_dir = "/old-project"
    monkeypatch.setattr(legacy, "list_instances", lambda **kw: [record()])
    legacy.discover_instance()
    assert legacy._legacy_fallback and legacy.remote_port == 61000


def test_record_at_old_port_with_wrong_directory_cannot_downgrade(legacy, monkeypatch):
    legacy._expected_work_dir = "/old-project"
    monkeypatch.setattr(legacy, "list_instances", lambda **kw: [record(61000)])
    with pytest.raises(InstanceSelectionError):
        legacy.discover_instance(provision=False)


@pytest.mark.parametrize("kind", ["registry", "ambiguous", "explicit", "saved"])
def test_fallback_never_hides_discovery_failures(legacy, monkeypatch, kind):
    if kind == "registry":
        def failed(**kw):
            raise DiscoveryError("registry unreadable")
        monkeypatch.setattr(legacy, "list_instances", failed)
    elif kind == "ambiguous":
        monkeypatch.setattr(legacy, "list_instances", lambda **kw: [record(), record(61002, 20)])
    elif kind == "explicit":
        legacy._selected_port = 61003
    else:
        legacy.instance_record = record()
    with pytest.raises((DiscoveryError, InstanceSelectionError)):
        legacy.discover_instance(provision=False)
    assert not legacy._legacy_fallback


@pytest.mark.parametrize("wrong_token", [False, True])
def test_old_signed_daemon_acceptance_still_requires_auth(legacy, monkeypatch, wrong_token):
    from tests.test_daemon_auth import _AuthDaemon, TOKEN, WRONG_TOKEN
    peer = _AuthDaemon()
    legacy._legacy_port = peer.port
    legacy.discover_instance()
    try:
        client = VirtuosoClient(port=peer.port, tunnel=legacy,
                                daemon_token=WRONG_TOKEN if wrong_token else TOKEN)
        client.use_current_selection()
        if wrong_token:
            with pytest.raises(daemon_auth.DaemonAuthError):
                client._ensure_daemon_capabilities(time.monotonic() + 3)
        else:
            assert client._ensure_daemon_capabilities(time.monotonic() + 3)["auth"] == "on"
        assert not peer.executed
    finally:
        peer.close()


def test_registered_peer_cannot_hide_behind_legacy_fallback(legacy):
    from tests.test_daemon_auth import _AuthDaemon, TOKEN
    peer = _AuthDaemon(caps_body=json.dumps(dict(proto=1, auth="on", daemon="ramic-bridge",
                                               virtuoso_pid=10, instance_record=record())))
    legacy._legacy_port = peer.port
    legacy.discover_instance()
    try:
        client = VirtuosoClient(port=peer.port, tunnel=legacy, daemon_token=TOKEN)
        client.use_current_selection()
        with pytest.raises(daemon_auth.DaemonAuthError, match="absent from discovery"):
            client._ensure_daemon_capabilities(time.monotonic() + 3)
        assert not peer.executed
    finally:
        peer.close()


def test_lazy_handshake_discovers_before_using_old_endpoint(legacy, monkeypatch):
    from tests.test_daemon_auth import _AuthDaemon, TOKEN
    peer = _AuthDaemon()
    legacy._legacy_port = peer.port
    monkeypatch.setattr(legacy, "ensure_daemon_token", lambda: TOKEN)
    try:
        client = VirtuosoClient(port=1, tunnel=legacy)
        assert client._ensure_daemon_capabilities(time.monotonic() + 3)["auth"] == "on"
        assert legacy._legacy_fallback and client._port == peer.port
        assert not peer.executed
    finally:
        peer.close()


def test_factory_with_old_saved_state_resolves_discovery_before_token_mode(legacy, monkeypatch):
    import virtuoso_bridge.virtuoso.basic.bridge as bridge
    from tests.test_daemon_auth import _AuthDaemon, TOKEN
    peer = _AuthDaemon()
    legacy._legacy_port = peer.port
    monkeypatch.setattr(bridge, "load_vb_env", lambda: None)
    monkeypatch.setattr(bridge, "resolve_profile", lambda profile: profile)
    monkeypatch.setattr(tunnel.SSHClient, "is_running", lambda *a: True)
    monkeypatch.setattr(tunnel.SSHClient, "read_state", lambda *a, **kw: {"port": peer.port})
    monkeypatch.setattr(tunnel.SSHClient, "from_env", lambda **kw: legacy)
    monkeypatch.setattr(bridge, "_acquire_daemon_token", lambda *a: pytest.fail("credential chosen before discovery"))
    modes = []
    def token():
        modes.append(legacy._legacy_fallback)
        return TOKEN
    monkeypatch.setattr(legacy, "ensure_daemon_token", token)
    monkeypatch.setattr(VirtuosoClient, "_reject_cross_user_daemon_if_reachable",
                        lambda self, **kw: self._ensure_daemon_capabilities(time.monotonic() + 3))
    try:
        client = VirtuosoClient.from_env()
        assert client.port == peer.port and modes == [True]
        assert not peer.executed
    finally:
        peer.close()


@pytest.mark.parametrize("outcome", ["success", "pid", "birth", "auth", "timeout"])
def test_legacy_restart_reconciles_once_without_replaying(legacy, monkeypatch, outcome):
    import virtuoso_bridge.virtuoso.basic.bridge as bridge
    clock = [100.0]
    monkeypatch.setattr(tunnel.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tunnel.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    legacy.discover_instance()
    monkeypatch.setattr(legacy, "read_daemon_token", lambda: "ab" * 32)
    births = iter(["birth", "changed"] if outcome == "birth" else ["birth"] * 100)
    monkeypatch.setattr(legacy, "_legacy_ciw_birth", lambda *a: next(births))
    saved = []
    monkeypatch.setattr(legacy, "save_state", lambda: saved.append(True))
    requests = []
    class Client:
        def __init__(self, **kw):
            pass
        def use_current_selection(self):
            pass
        def _ensure_daemon_capabilities(self, deadline, refresh=False):
            if refresh and outcome == "auth":
                raise daemon_auth.DaemonAuthError("wrong token")
            return dict(auth="on", virtuoso_pid=20 if refresh and outcome == "pid" else 10,
                        daemon_instance="new" if refresh and outcome != "timeout" else "old")
        def execute_skill(self, script, **kw):
            requests.append((script, kw))
            # Even an uncertain application reply must reconcile, never replay.
            return SimpleNamespace(status="unknown", errors=["reply lost"])
    monkeypatch.setattr(bridge, "VirtuosoClient", Client)
    if outcome == "success":
        legacy._restart_legacy_instance(2)
        assert saved == [True]
    else:
        with pytest.raises((RuntimeError, TimeoutError, daemon_auth.DaemonAuthError)):
            legacy._restart_legacy_instance(2)
        assert not saved
    assert len(requests) == 1 and requests[0][1]["retry_connect"] is False
    assert clock[0] <= 102


def test_discovery_checks_deadline_after_unbounded_root_lookup(monkeypatch):
    from virtuoso_bridge.transport import remote_paths
    clock = [100.0]
    monkeypatch.setattr(tunnel.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(remote_paths, "load_vb_env", lambda: None)
    monkeypatch.setattr(remote_paths, "remote_scratch_root", lambda *a, **kw: "/tmp")
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    client = tunnel.SSHClient("fixture.invalid", discover_instances=True)
    budgets = []
    def run(command, timeout=None):
        budgets.append(timeout)
        clock[0] += 1
        return SimpleNamespace(returncode=0, stdout="fixture\n", stderr="")
    monkeypatch.setattr(client._ssh_runner, "run_command", run)
    with pytest.raises(TimeoutError, match="discovery deadline"):
        client.list_instances(timeout=0.5)
    assert budgets == [None]


@pytest.mark.skipif(os.name != "posix", reason="native POSIX permissions")
def test_historical_metadata_is_readable_but_new_identity_requires_private_modes(tmp_path):
    parent = tmp_path / "state"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    path = parent / "state.json"
    value = dict(mode="remote", port=61001, remote_port=61000, tunnel_pid=123,
                 remote_host="fixture", remote_user="owner")
    path.write_text(json.dumps(value))
    path.chmod(0o644)
    assert tunnel._read_private_json(path, legacy_metadata=True) == value
    with pytest.raises(ValueError):
        tunnel._read_private_json(path)
    value["tunnel_identity"] = "new birth identity"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="historical"):
        tunnel._read_private_json(path, legacy_metadata=True)
    path.chmod(0o666)
    with pytest.raises(ValueError):
        tunnel._read_private_json(path, legacy_metadata=True)


def test_plain_stop_reports_unsafe_retained_state(monkeypatch, capsys):
    from virtuoso_bridge import cli
    monkeypatch.setattr(tunnel.SSHClient, "is_running", lambda *a: False)
    monkeypatch.setattr(tunnel.SSHClient, "inspect_states", lambda *a: [{"path": "/state/old.json", "error": "unsafe"}])
    assert cli._stop_one() == 1
    assert "/state/old.json" in capsys.readouterr().out


def test_unicode_paths_remain_readable_with_controls_escaped():
    output = format_instances([dict(record(), work_dir="/设计/模块\n\x1b\u202e")])
    assert "/设计/模块" in output
    assert "\\n\\x1b\\u202e" in output
    assert "\x1b" not in output

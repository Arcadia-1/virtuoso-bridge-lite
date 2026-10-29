"""Behavioral regressions for the approved instance-discovery audit fixes."""
import json
import os
from types import SimpleNamespace
import time

import pytest

from virtuoso_bridge import cli, daemon_auth
from virtuoso_bridge.instance_discovery import DiscoveryError, InstanceSelectionError, compatibility_message
from virtuoso_bridge.transport import tunnel
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
from tests.test_instance_discovery import row


@pytest.mark.parametrize("record", [[], ["truthy"], "record", 1, True, {}, row(port=True), row(pid=-1)])
def test_invalid_expected_record_is_rejected_before_network(record, monkeypatch):
    client = VirtuosoClient(tunnel=SimpleNamespace(_discover_instances=True, instance_record=record))
    monkeypatch.setattr(client, "_exchange_payload", lambda *a: pytest.fail("network reached"))
    with pytest.raises(daemon_auth.DaemonAuthError):
        client._ensure_daemon_capabilities(time.monotonic() + 2)


def test_selector_validation_precedes_remote_work(monkeypatch):
    client = tunnel.SSHClient(remote_host="fixture", discover_instances=True, expected_work_dir="relative")
    monkeypatch.setattr(client, "list_instances", lambda: pytest.fail("remote operation"))
    with pytest.raises(InstanceSelectionError, match="absolute"):
        client.discover_instance()


@pytest.mark.parametrize("failure", [None, DiscoveryError("registry unavailable"), InstanceSelectionError("ambiguous")])
def test_status_always_attempts_spectre_diagnostics(monkeypatch, failure):
    client = tunnel.SSHClient(remote_host="fixture", discover_instances=True)
    def discover():
        if failure:
            raise failure
        return []
    calls = []
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_get_cli_profile", lambda: None)
    monkeypatch.setattr(tunnel.SSHClient, "from_env", lambda **kw: client)
    monkeypatch.setattr(tunnel.SSHClient, "running_targets", lambda: [])
    monkeypatch.setattr(client, "list_instances", discover)
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(cli, "_print_spectre_status", lambda *a: calls.append(a))
    assert cli._print_status() == 1
    assert len(calls) == 1


def test_selection_write_failure_keeps_previous_complete_file(tmp_path, monkeypatch):
    path = tmp_path / "state" / "instance.json"
    tunnel._write_private_json(path, {"previous": True})
    def fail(*args):
        raise OSError("replacement refused")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        tunnel._write_private_json(path, {"next": True})
    assert json.loads(path.read_text()) == {"previous": True}
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("payload", ["{", "[]", '{"record": "truthy"}'])
def test_corrupt_selection_is_not_silently_absent(tmp_path, monkeypatch, payload):
    path = tmp_path / "selection.json"
    path.write_text(payload)
    monkeypatch.setattr(tunnel, "_selection_file", lambda *a: path)
    with pytest.raises(InstanceSelectionError, match="Invalid saved"):
        tunnel._read_selection()


def test_concurrent_state_writes_publish_only_complete_json(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "state" / "selection.json"
    values = [{"writer": i, "payload": str(i) * 1000} for i in range(12)]
    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(lambda value: tunnel._write_private_json(path, value), values))
    assert json.loads(path.read_text()) in values
    assert list(path.parent.iterdir()) == [path]


def test_unsupported_local_discovery_explains_linux_requirement(monkeypatch):
    monkeypatch.setattr(tunnel.sys, "platform", "darwin")
    client = tunnel.SSHClient(remote_host="localhost", discover_instances=True)
    with pytest.raises(DiscoveryError, match="requires Linux"):
        client.list_instances()


def test_stale_unscoped_selection_never_retargets(monkeypatch):
    client = tunnel.SSHClient(remote_host="fixture", discover_instances=True)
    client.instance_record = row()
    monkeypatch.setattr(client, "list_instances", lambda: [row(65002, 20)])
    monkeypatch.setattr(client, "ensure_tunnel", lambda: pytest.fail("new tunnel"))
    with pytest.raises(InstanceSelectionError, match="select explicitly"):
        client.reconcile_instance()


def test_unique_project_replacement_is_verified_before_state_publication(monkeypatch):
    client = tunnel.SSHClient(remote_host="fixture", discover_instances=True, expected_work_dir="/project/a")
    client.instance_record = row()
    replacement = row(65002, 20)
    monkeypatch.setattr(client, "list_instances", lambda: [replacement])
    monkeypatch.setattr(client, "read_daemon_token", lambda: "ab" * 32)
    actions = []
    monkeypatch.setattr(client, "ensure_tunnel", lambda: actions.append("forward"))
    monkeypatch.setattr(client, "_verify_discovered_instance", lambda *a: actions.append("verify"))
    monkeypatch.setattr(client, "save_state", lambda: actions.append("save"))
    assert client.reconcile_instance()
    assert client.instance_record == replacement
    assert actions == ["forward", "verify", "save"]


@pytest.mark.parametrize("caps,fragment", [({}, "unknown (older daemon)"), ({"build": "older"}, "differs"),
                                          ({"build": "instance-discovery-3"}, "daemon build:")])
def test_build_report_preserves_compatible_old_daemons(caps, fragment):
    assert fragment in compatibility_message(caps)


def test_shared_setup_no_match_does_not_deploy(monkeypatch):
    client = tunnel.SSHClient(remote_host="fixture", discover_instances=True, shared_setup="/opt/shared/setup.il")
    monkeypatch.setattr(client, "list_instances", lambda: [])
    monkeypatch.setattr(client, "_instance_root", lambda **kw: "/private")
    monkeypatch.setattr(client, "ensure_remote_setup", lambda: pytest.fail("deployment"))
    with pytest.raises(InstanceSelectionError, match='/opt/shared/setup.il'):
        client.discover_instance()


def test_explicit_stop_targets_saved_endpoint_without_discovery(monkeypatch):
    calls = []
    client = SimpleNamespace(target="fixture@host:65002", stop=lambda: calls.append("stop"),
                             close=lambda: calls.append("close"))
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_get_cli_profile", lambda: "fixture")
    def factory(**kwargs):
        assert kwargs["selected_port"] == 65002 and kwargs["read_selection"] is False
        return client
    monkeypatch.setattr(tunnel.SSHClient, "from_env", factory)
    def state(profile, *, target):
        assert profile == "fixture" and target == client.target
        return {"fixture": True}
    monkeypatch.setattr(tunnel.SSHClient, "read_state", state)
    monkeypatch.setattr(tunnel.SSHClient, "_state_is_live", lambda value: value == {"fixture": True})
    assert cli.cli_stop(65002) == 0
    assert calls == ["stop", "close"]

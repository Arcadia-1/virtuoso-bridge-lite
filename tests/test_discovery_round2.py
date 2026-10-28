"""Behavioral coverage for the second instance-discovery audit response."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from virtuoso_bridge import cli
from virtuoso_bridge.instance_discovery import DAEMON_BUILD, InstanceSelectionError
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport import ssh, tunnel
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
from tests.test_instance_discovery import row


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("rows", [[], [row(), row(65002, 20)]])
def test_sessions_dispatch_lists_without_provisioning(monkeypatch, capsys, rows, json_output):
    client = tunnel.SSHClient("fixture", discover_instances=True)
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(tunnel.SSHClient, "from_env", lambda **kw: client)
    monkeypatch.setattr(client, "list_instances", lambda: rows)
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    for method in ("warm", "ensure_remote_setup", "ensure_daemon_token", "save_state"):
        monkeypatch.setattr(client, method, lambda *a: pytest.fail("listing mutated state"))
    assert cli.main(["sessions"] + (["--json"] if json_output else [])) == 0
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(output) == rows
    else:
        assert "Virtuoso PID" in output
        for record in rows:
            assert str(record["port"]) in output
    assert closed == [True] and client.instance_record is None


@pytest.mark.skipif(sys.platform != "linux", reason="native registry and registered CLI on Linux")
@pytest.mark.parametrize("count", [0, 2])
def test_registered_sessions_json_with_real_locked_records(tmp_path, count):
    from virtuoso_bridge.virtuoso.basic.resources import ramic_registry
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    sockets, leases = [], []
    try:
        for _ in range(count):
            sock = socket.socket()
            sockets.append(sock)
            leases.append(ramic_registry.bind_instance(sock, "127.0.0.1", directory=str(root / "runs")))
        config = tmp_path / "bridge.env"
        config.write_text("VB_REMOTE_HOST=localhost\nVB_REMOTE_SCRATCH_ROOT=" + str(root) + "\n")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("VB_")}
        environment.update(VB_STATE_DIR=str(tmp_path / "state"), VB_CACHE_DIR=str(tmp_path / "cache"))
        command = Path(sys.executable).parent / "virtuoso-bridge"
        result = subprocess.run([str(command), "sessions", "--json", "--env", str(config)],
                                capture_output=True, text=True, env=environment, cwd=tmp_path, timeout=15)
        assert result.returncode == 0, result.stderr
        records = json.loads(result.stdout)
        assert {r["port"] for r in records} == {s.getsockname()[1] for s in sockets}
        assert not (tmp_path / "state").exists()
        assert not (root / "daemon").exists()
        assert not list(root.glob("**/*request*"))
    finally:
        for lease in leases:
            lease.close()
        for sock in sockets:
            sock.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_local_screenshot_expands_creates_and_copies(tmp_path, monkeypatch, explicit):
    home = tmp_path / "local home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/images with spaces" if explicit else "~/.virtuoso-bridge")
    client = VirtuosoClient()
    seen = []
    def image_writer(code, **kwargs):
        path = Path(re.search(r'\?path "([^"]+)"', code).group(1))
        assert path.is_absolute() and path.parent.is_dir()
        assert path.is_relative_to(home)
        path.write_bytes(b"synthetic image")
        seen.append(path)
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")
    monkeypatch.setattr(client, "execute_skill", image_writer)
    output = tmp_path / "download.png"
    result = client.screenshot(output)
    assert result.status == ExecutionStatus.SUCCESS, result
    assert output.read_bytes() == b"synthetic image"
    assert len(seen) == 1


def test_daemon_build_literals_match_client_without_importing_workers():
    root = Path(tunnel._find_ramic_helper("ramic_bridge_daemon_3.py")).parent
    for name in ("ramic_bridge_daemon_3.py", "ramic_bridge_daemon_27.py"):
        tree = ast.parse((root / name).read_text())
        builds = [value.value for node in ast.walk(tree) if isinstance(node, ast.Dict)
                  for key, value in zip(node.keys, node.values)
                  if isinstance(key, ast.Constant) and key.value == "build" and isinstance(value, ast.Constant)]
        assert builds == [DAEMON_BUILD]


def test_legacy_forward_parser_rejects_extra_work_and_config():
    args = ["ssh", "-F", "/dev/null", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ControlPersist=no", "-N", "-L", "12345:127.0.0.1:23456", "user@host"]
    assert ssh._legacy_forward_argv_matches(args, "user@host", 12345, 23456)
    for changed in (args[:-1] + ["other@host"], args[:2] + ["custom.conf"] + args[3:],
                    args[:-1] + ["-L", "34567:127.0.0.1:45678", args[-1]],
                    args[:-1] + ["-R", "34567:127.0.0.1:45678", args[-1]],
                    args[:-1] + ["-o", "ProxyCommand=arbitrary", args[-1]],
                    args + ["echo", "unsafe"]):
        assert not ssh._legacy_forward_argv_matches(changed, "user@host", 12345, 23456)


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner/mode and special files")
@pytest.mark.parametrize("kind", ["mode", "parent", "symlink", "fifo", "wrong_owner"])
def test_private_state_reader_rejects_untrusted_files(tmp_path, monkeypatch, kind):
    path = tmp_path / "state.json"
    tunnel._write_private_json(path, {"fixture": True})
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "parent":
        tmp_path.chmod(0o755)
    elif kind == "wrong_owner":
        monkeypatch.setattr(tunnel.os, "geteuid", lambda: path.stat().st_uid + 1)
    else:
        path.unlink()
        if kind == "symlink":
            other = tmp_path / "other"
            other.write_text("{}")
            path.symlink_to(other)
        else:
            os.mkfifo(path)
    with pytest.raises(ValueError):
        tunnel._read_private_json(path)


def test_inspect_state_does_not_construct_transport(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(tunnel, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(tunnel, "_legacy_state_files", lambda profile: [])
    monkeypatch.setattr(tunnel, "_selection_file", lambda profile: tmp_path / "missing")
    monkeypatch.setattr(tunnel.SSHClient, "from_env", lambda **kw: pytest.fail("transport constructed"))
    path = tmp_path / "state-old.json"
    state = dict(mode="remote", port=12345, remote_port=23456, tunnel_pid=123,
                 daemon_host="fixture", target="fixture:23456", daemon_token="never show this")
    tunnel._write_private_json(path, state)
    assert cli.main(["status", "--inspect-state"]) == 0
    result = capsys.readouterr().out
    assert "never show this" not in result
    assert json.loads(result)[0]["path"] == str(path)
    assert json.loads(result)[0]["birth_identity_saved"] is False


@pytest.fixture
def restarting(monkeypatch):
    client = tunnel.SSHClient("localhost", discover_instances=True)
    original = row()
    replacement = dict(row(65002), instance="a" * 32, daemon_pid=12)
    client.instance_record = original
    calls = []
    monkeypatch.setattr(client, "ciw_process_identity", lambda r: "boot:birth")
    monkeypatch.setattr(client, "list_instances", lambda: [original, row(65003, 20)] if not calls else [replacement, row(65003, 20)])
    monkeypatch.setattr(client, "read_daemon_token", lambda: "ab" * 32)
    monkeypatch.setattr(client, "verify_instance", lambda timeout: calls.append(("verify", client.remote_port)))
    published = []
    monkeypatch.setattr(client, "save_state", lambda: published.append(client.instance_record))
    monkeypatch.setattr(client, "_clear_state", lambda: pytest.fail("selection cleared before verification"))
    monkeypatch.setattr(VirtuosoClient, "_ensure_daemon_capabilities", lambda *a: {"lane": {"state": "idle"}})
    def execute(self, code, **kwargs):
        assert kwargs["retry_connect"] is False
        assert code == 'hiRegTimer("RBSetPort(RBConfiguredPort)" 2)'
        calls.append(("request", code))
        return VirtuosoResult(status=ExecutionStatus.ERROR, errors=["Empty response from daemon"])
    monkeypatch.setattr(VirtuosoClient, "execute_skill", execute)
    return client, original, replacement, calls, published


def test_restart_preserves_ciw_choice_and_authenticates_before_publication(restarting):
    client, original, replacement, calls, published = restarting
    client.restart_instance(timeout=3)
    assert published == [replacement]
    assert [action[0] for action in calls] == ["verify", "request", "verify"]
    assert client.instance_record["pid"] == original["pid"]
    assert client.remote_port == replacement["port"]


def test_explicit_port_can_replace_a_different_still_live_saved_choice():
    old, chosen = row(), row(65002, 20)
    client = tunnel.SSHClient("fixture", discover_instances=True, selected_port=chosen["port"])
    client.instance_record = old
    assert client.select_record([old, chosen]) == chosen


def test_restart_never_relaxes_an_exact_selector(restarting):
    client, original, _, calls, published = restarting
    client._selected_port = original["port"]
    with pytest.raises(InstanceSelectionError, match="exact VB_REMOTE_PORT"):
        client.restart_instance(timeout=3)
    assert not published
    assert len([c for c in calls if c[0] == "request"]) == 1


def test_restart_refuses_recycled_ciw_before_request(restarting, monkeypatch):
    client, _, _, calls, published = restarting
    identities = iter(["birth1", "birth2"])
    monkeypatch.setattr(client, "ciw_process_identity", lambda r: next(identities))
    with pytest.raises(RuntimeError, match="identity changed"):
        client.restart_instance(timeout=3)
    assert not published and not any(c[0] == "request" for c in calls)


def test_restart_timeout_does_not_replay_or_publish(restarting, monkeypatch):
    client, original, _, calls, published = restarting
    monkeypatch.setattr(client, "list_instances", lambda: [original, row(65003, 20)])
    with pytest.raises(RuntimeError, match="requested once"):
        client.restart_instance(timeout=0.1)
    assert not published
    assert len([c for c in calls if c[0] == "request"]) == 1


def test_restart_auth_failure_never_publishes_replacement(restarting, monkeypatch):
    client, _, _, calls, published = restarting
    def verify(timeout):
        if calls:
            raise RuntimeError("signed record mismatch")
        calls.append(("verify", client.remote_port))
    monkeypatch.setattr(client, "verify_instance", verify)
    with pytest.raises(RuntimeError, match="signed record mismatch"):
        client.restart_instance(timeout=3)
    assert not published


def test_automatic_stop_never_deletes_shared_bundle(monkeypatch):
    client = tunnel.SSHClient("fixture", discover_instances=True, keep_remote_files=False)
    client._remote_setup_done = True
    client._remote_work_dir = "/private/daemon"
    client._ssh_runner = SimpleNamespace(is_tunnel_alive=False, stop_port_forward=lambda: None)
    monkeypatch.setattr(client, "read_state", lambda *a, **kw: None)
    monkeypatch.setattr(client, "_require_deployment_runner", lambda: pytest.fail("shared bundle cleanup"))
    monkeypatch.setattr(client, "_clear_state", lambda: None)
    client.stop()


def test_no_record_status_explains_where_to_get_load_instruction(monkeypatch, capsys):
    client = tunnel.SSHClient("fixture", discover_instances=True)
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(tunnel.SSHClient, "from_env", lambda **kw: client)
    monkeypatch.setattr(client, "list_instances", lambda: [])
    monkeypatch.setattr(tunnel.SSHClient, "running_targets", lambda: [])
    monkeypatch.setattr(cli, "_print_spectre_status", lambda *a: None)
    assert cli._print_status() == 1
    assert "start for the load instruction" in capsys.readouterr().out


def test_legacy_bootstrap_retains_banner_host_mismatch_guidance(monkeypatch, capsys):
    from virtuoso_bridge.virtuoso import x11
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_get_cli_profile", lambda: None)
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (None, "fixture"))
    monkeypatch.setattr(x11, "bootstrap_ciw", lambda *a, **kw: [{"pid": 10}])
    state = {"port": 65001, "setup_path": "/synthetic/setup.il", "daemon_endpoint_hostname": "expected"}
    monkeypatch.setattr(tunnel.SSHClient, "read_state", lambda *a: state)
    bridge = SimpleNamespace(port=65001, daemon_host="expected", ensure_daemon_token=lambda: "ab" * 32,
                             read_daemon_identity=lambda: {"host": "wrong-host"}, close=lambda: None)
    monkeypatch.setattr(tunnel.SSHClient, "from_env", lambda **kw: bridge)
    monkeypatch.setattr(VirtuosoClient, "test_connection", lambda *a, **kw: False)
    assert cli.cli_bootstrap(window_id="0x100", timeout=0) == 1
    output = capsys.readouterr().out
    assert "banner host: wrong-host" in output
    assert "VB_DAEMON_HOST=wrong-host" in output


def test_state_cleanup_failure_after_verified_stop_is_nonfatal(tmp_path, monkeypatch, caplog):
    client = tunnel.SSHClient("localhost")
    monkeypatch.setattr(tunnel, "_state_file", lambda *a: tmp_path / "state.json")
    monkeypatch.setattr(tunnel, "_selection_file", lambda *a: tmp_path / "selection.json")
    monkeypatch.setattr(tunnel, "_legacy_state_files", lambda *a: [])
    tunnel._write_private_json(tmp_path / "state.json", dict(mode="local", port=65432))
    def denied(*a, **kw):
        raise PermissionError("fixture locked file")
    monkeypatch.setattr(Path, "unlink", denied)
    client.stop()
    assert "could not safely clear state" in caplog.text
    assert (tmp_path / "state.json").exists()

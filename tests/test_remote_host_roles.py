from __future__ import annotations

import json
import os
import sys

import pytest

from virtuoso_bridge.daemon_guard import check_daemon_host
from virtuoso_bridge.spectre.runner import SpectreSimulator
from virtuoso_bridge.transport.remote_roles import remote_host_roles_from_os
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.transport.tunnel import SSHClient


def _clear_role_env(monkeypatch, profile: str | None = None) -> None:
    suffix = f"_{profile}" if profile else ""
    for name in (
        "VB_REMOTE_HOST",
        "VB_GUI_HOST",
        "VB_DEPLOY_HOST",
        "VB_DAEMON_HOST",
        "VB_SPECTRE_HOST",
        "VB_REMOTE_USER",
        "VB_JUMP_HOST",
        "VB_JUMP_USER",
    ):
        monkeypatch.delenv(f"{name}{suffix}", raising=False)


def test_legacy_remote_host_populates_every_role(monkeypatch) -> None:
    _clear_role_env(monkeypatch)
    monkeypatch.setenv("VB_REMOTE_HOST", "compute")

    roles = remote_host_roles_from_os(load=False)

    assert roles.gui_host == "compute"
    assert roles.deploy_host == "compute"
    assert roles.daemon_host == "compute"
    assert roles.spectre_host == "compute"


def test_split_hosts_do_not_require_legacy_remote_host(monkeypatch) -> None:
    _clear_role_env(monkeypatch)
    monkeypatch.setenv("VB_GUI_HOST", "gui-a")
    monkeypatch.setenv("VB_DAEMON_HOST", "compute-b")
    monkeypatch.setenv("VB_SPECTRE_HOST", "spectre-c")
    monkeypatch.setenv("VB_JUMP_HOST", "gui-a")

    roles = remote_host_roles_from_os(load=False)

    assert roles.legacy_host is None
    assert roles.gui_host == "gui-a"
    assert roles.deploy_host == "gui-a"
    assert roles.daemon_host == "compute-b"
    assert roles.spectre_host == "spectre-c"
    assert roles.jump_for("gui-a") is None
    assert roles.jump_for("compute-b") == "gui-a"


def test_ssh_client_assigns_role_runners_and_suppresses_self_jump(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _Runner:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)
            self.host = str(kwargs["host"])

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHRunner", _Runner)

    client = SSHClient(
        remote_host="legacy",
        daemon_host="compute-b",
        deploy_host="gui-a",
        gui_host="gui-a",
        spectre_host="spectre-c",
        remote_user="designer",
        jump_host="gui-a",
    )

    assert client.ssh_runner.host == "compute-b"
    assert client.deployment_runner.host == "gui-a"
    assert client.gui_runner is client.deployment_runner
    assert client.spectre_runner.host == "spectre-c"
    by_host = {str(item["host"]): item for item in captured}
    assert by_host["compute-b"]["jump_host"] == "gui-a"
    assert by_host["gui-a"]["jump_host"] is None
    assert by_host["spectre-c"]["jump_host"] == "gui-a"


def test_daemon_host_check_accepts_fqdn_and_flags_real_mismatch() -> None:
    assert check_daemon_host(
        daemon_hostname="compute-b",
        endpoint_hostname="compute-b.example.edu",
        configured_host="compute-alias",
    ).ok

    mismatch = check_daemon_host(
        daemon_hostname="compute-b",
        endpoint_hostname="gui-a.example.edu",
        configured_host="gui-a",
    )
    assert not mismatch.ok
    assert "compute-b" in mismatch.error
    assert "gui-a" in mismatch.error


def test_split_setup_uploads_on_deploy_host_and_checks_daemon_visibility(monkeypatch) -> None:
    runners = {}

    class _Runner:
        def __init__(self, **kwargs) -> None:
            self.host = str(kwargs["host"])
            self.commands = []
            self.uploads = {}
            runners[self.host] = self

        def run_command(self, command, timeout=None):
            self.commands.append(command)
            if "python3 --version" in command:
                return CommandResult(0, "Python 3.11.8\nCMD:python3\n", "")
            return CommandResult(0, "", "")

        def upload_text(self, text, remote_path, timeout=None):
            self.uploads[remote_path] = text
            return CommandResult(0, "", "")

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHRunner", _Runner)
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/shared/bridge")
    monkeypatch.setenv("VB_CLIENT_ID", "laptop")

    client = SSHClient(
        remote_host="legacy",
        daemon_host="compute-b",
        deploy_host="gui-a",
        gui_host="gui-a",
        remote_user="designer",
    )
    client.ensure_remote_setup()

    assert runners["gui-a"].uploads
    assert not runners["compute-b"].uploads
    assert any("python3 --version" in cmd for cmd in runners["compute-b"].commands)
    assert any("test -r /shared/bridge/" in cmd for cmd in runners["compute-b"].commands)
    setup = next(
        text for path, text in runners["gui-a"].uploads.items()
        if path.endswith("/virtuoso_setup.il")
    )
    assert 'setShellEnvVar("RB_IDENTITY_PATH" "/shared/bridge/' in setup
    owner_path = next(path for path in runners["gui-a"].uploads
                      if path.endswith("/ramic_owner.py"))
    assert "class OwnerProcess" in runners["gui-a"].uploads[owner_path]
    assert f"test -r {owner_path}" in runners["compute-b"].commands


def test_spectre_role_runs_without_bridge_tunnel_state(monkeypatch) -> None:
    _clear_role_env(monkeypatch)
    monkeypatch.setenv("VB_GUI_HOST", "gui-a")
    monkeypatch.setenv("VB_DAEMON_HOST", "compute-b")
    monkeypatch.setenv("VB_SPECTRE_HOST", "spectre-c")
    monkeypatch.setenv("VB_REMOTE_USER", "designer")
    monkeypatch.setattr("virtuoso_bridge.spectre.runner.load_vb_env", lambda: None)

    class _NoTunnel:
        @staticmethod
        def is_running(profile=None):
            return False

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _NoTunnel)

    simulator = SpectreSimulator.from_env()

    assert simulator._remote_host == "spectre-c"
    assert simulator._remote_user == "designer"
    assert simulator._ssh_runner is None


@pytest.fixture
def split_hosts(monkeypatch):
    """Three hosts with different Python paths and colliding numeric PIDs."""
    from tests.test_instance_discovery import row
    from virtuoso_bridge.transport import tunnel
    runners = {}

    class Runner:
        persistent_shell_enabled = False
        is_tunnel_alive = False

        def __init__(self, **kwargs):
            self.host = kwargs["host"]
            self.user = kwargs.get("user")
            self.commands, self.uploads = [], {}
            self.rows = [row()]
            self.birth = [self.host + "-boot", self.host + "-birth"]
            self.readable = True
            runners[self.host] = self

        def run_command(self, command, **kwargs):
            self.commands.append(command)
            if "VB_BUNDLE_" in command:
                assert command.startswith("/deploy/python -c ")
                return CommandResult(0, '{"reused": false}', "")
            if "CMD:" in command:
                return CommandResult(0, "Python 3.11.8\nCMD:/" + self.host + "/python\n", "")
            if "identity = _identity(pid)" in command:
                return CommandResult(0, json.dumps(self.birth), "")
            if "--read-only" in command:
                return CommandResult(0, json.dumps(self.rows), "")
            if command.startswith("test -r "):
                return CommandResult(0 if self.readable else 1, "", "fixture: hidden path")
            return CommandResult(0, "", "")

        def upload_text(self, text, remote_path, **kwargs):
            self.uploads[remote_path] = text
            return CommandResult(0, "", "")

    monkeypatch.setattr(tunnel, "SSHRunner", Runner)
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/shared/fixture")
    client = SSHClient("daemon", daemon_host="daemon", gui_host="gui", deploy_host="deploy",
                       remote_user="fixture", discover_instances=True)
    client.gui_runner
    return client, runners


@pytest.mark.parametrize("discover", [False, True])
def test_three_host_setup_uses_each_hosts_interpreter_and_visibility(split_hosts, discover):
    client, runners = split_hosts
    client._discover_instances = discover
    client.ensure_remote_setup()
    assert bool(runners["deploy"].uploads) is not discover
    assert not runners["gui"].uploads and not runners["daemon"].uploads
    assert any(command.startswith("test -r ") for command in runners["gui"].commands)
    assert any(command.startswith("test -r ") for command in runners["daemon"].commands)
    if discover:
        bundle = json.loads(next(command for command in runners["deploy"].commands if "VB_BUNDLE_" in command).splitlines()[-2])
        setup = bundle["files"]["virtuoso_setup.il"]
        assert 'setShellEnvVar("RB_PYTHON_PATH" "/daemon/python")' in setup
        assert 'setShellEnvVar("RB_SCRATCH_ROOT" "/shared/fixture")' in setup
        assert any(command.startswith("/deploy/python -c ") for command in runners["deploy"].commands)


@pytest.mark.parametrize("hidden_host", ["gui", "daemon"])
def test_three_host_generated_setup_refuses_invisible_bundle(split_hosts, hidden_host):
    client, runners = split_hosts
    runners[hidden_host].readable = False
    with pytest.raises(RuntimeError, match="not visible"):
        client.ensure_remote_setup()
    assert not client._remote_setup_done


def test_three_host_discovery_runs_only_on_daemon(split_hosts):
    client, runners = split_hosts
    assert client.list_instances() == runners["daemon"].rows
    assert not runners["gui"].commands and not runners["deploy"].commands


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("local_daemon", [False, True])
def test_ciw_birth_uses_gui_even_when_daemon_pid_collides(split_hosts, legacy, local_daemon):
    client, runners = split_hosts
    if local_daemon:
        client._ssh_runner = None
    record = runners["daemon"].rows[0]
    birth = (client._legacy_ciw_birth(record["pid"], 5) if legacy else
             client.ciw_process_identity(record))
    assert birth == (["gui-boot", "gui-birth"] if legacy else "gui-boot:gui-birth")
    assert not runners["daemon"].commands and not runners["deploy"].commands


@pytest.mark.parametrize("legacy", [False, True])
def test_three_host_restart_preserves_peer_and_checks_gui_identity(split_hosts, monkeypatch, legacy):
    from virtuoso_bridge import VirtuosoClient
    from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
    from tests.test_instance_discovery import row
    client, runners = split_hosts
    original, peer = row(), row(65003, 20)
    replacement = dict(row(65002), instance="a" * 32, daemon_pid=12)
    client._legacy_port = original["port"] if legacy else None
    client.instance_record = None if legacy else original
    runners["daemon"].rows = [] if legacy else [original, peer]
    actions = []
    monkeypatch.setattr(client, "read_daemon_token", lambda: "ab" * 32)
    monkeypatch.setattr(client, "read_state", lambda *a, **kw: None)
    monkeypatch.setattr(client, "ensure_tunnel", lambda: actions.append(("tunnel", client.ssh_runner.host)))
    monkeypatch.setattr(client, "verify_instance", lambda timeout: actions.append(("verify", client.remote_port)))
    monkeypatch.setattr(client, "save_state", lambda: actions.append(("save", client.instance_record)))
    requested = []

    def capabilities(*args, **kwargs):
        return dict(auth="on", virtuoso_pid=10, daemon_instance=("b" if requested else "a") * 32,
                    lane={"state": "idle"})

    def execute(self, code, **kwargs):
        assert kwargs["retry_connect"] is False
        requested.append(code)
        if not legacy:
            runners["daemon"].rows = [replacement, peer]
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")

    monkeypatch.setattr(VirtuosoClient, "_ensure_daemon_capabilities", capabilities)
    monkeypatch.setattr(VirtuosoClient, "execute_skill", execute)
    client.restart_instance(timeout=3)
    assert len(requested) == 1
    assert actions[-1] == ("save", None if legacy else replacement)
    assert all(value == "daemon" for kind, value in actions if kind == "tunnel")
    assert not any("identity = _identity(pid)" in command for command in runners["daemon"].commands)
    assert sum("identity = _identity(pid)" in command for command in runners["gui"].commands) >= 2
    assert not runners["deploy"].commands
    if not legacy:
        assert peer in runners["daemon"].rows


@pytest.mark.skipif(sys.platform != "linux", reason="native GUI /proc probe and POSIX shell")
@pytest.mark.parametrize("legacy", [False, True])
def test_gui_identity_probe_executes_real_shell(monkeypatch, legacy):
    from tests.test_remote_deploy_dirs import _LocalShellRunner
    from virtuoso_bridge.virtuoso.basic.resources.ramic_owner import _boot_id, _identity
    from tests.test_instance_discovery import row
    client = SSHClient("daemon", gui_host="gui", deploy_host="deploy")
    client._role_runners["gui"] = _LocalShellRunner()
    monkeypatch.setattr(client, "_detect_remote_python", lambda *a, **kw: (sys.executable, 3, 9))
    record = dict(row(pid=os.getpid(), work_dir=os.getcwd()), uid=os.geteuid() + 1)
    # The account's numeric UID on the daemon host is not the GUI host's UID.
    birth = (client._legacy_ciw_birth(record["pid"], 5) if legacy else
             client.ciw_process_identity(record))
    expected = [_boot_id(), _identity(os.getpid())]
    assert birth == (expected if legacy else ":".join(expected))

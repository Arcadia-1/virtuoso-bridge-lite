from __future__ import annotations

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

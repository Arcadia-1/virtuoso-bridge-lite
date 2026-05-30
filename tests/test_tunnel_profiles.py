from __future__ import annotations

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.transport.tunnel import (
    SSHClient,
    _profiled_bridge_leaf,
    _profiled_env_key,
)
from virtuoso_bridge import cli


class _FakeRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.uploads: dict[str, str] = {}

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        return CommandResult(returncode=0, stdout="", stderr="")

    def upload_text(self, text: str, remote_path: str, timeout=None) -> CommandResult:
        self.uploads[remote_path] = text
        return CommandResult(returncode=0, stdout="", stderr="")


def test_profiled_bridge_leaf_preserves_default_path() -> None:
    assert _profiled_bridge_leaf(None) == "virtuoso_bridge"


def test_profiled_bridge_leaf_adds_profile_suffix() -> None:
    assert _profiled_bridge_leaf("t28_digital") == "virtuoso_bridge_t28_digital"
    assert _profiled_bridge_leaf("t28/io") == "virtuoso_bridge_t28_io"


def test_profiled_env_key_preserves_default_and_suffixes_profiles() -> None:
    assert _profiled_env_key("VB_LOCAL_PORT", None) == "VB_LOCAL_PORT"
    assert _profiled_env_key("VB_LOCAL_PORT", "t180_io") == "VB_LOCAL_PORT_t180_io"


def test_remote_setup_path_and_port_are_profile_scoped(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    monkeypatch.delenv("VB_CLIENT_ID_t28_digital", raising=False)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    fake = _FakeRunner()
    client = SSHClient(
        remote_host="thu-wei",
        remote_user="designer",
        port=65263,
        profile="t28_digital",
    )
    client._ssh_runner = fake
    monkeypatch.setattr(client, "_detect_remote_python", lambda: ("python3", 3, 11))

    client.ensure_remote_setup()

    assert client.remote_work_dir == "/tmp/virtuoso_bridge_designer/90590/virtuoso_bridge_t28_digital"
    setup_path = "/tmp/virtuoso_bridge_designer/90590/virtuoso_bridge_t28_digital/virtuoso_setup.il"
    setup = fake.uploads[setup_path]
    assert 'setShellEnvVar("RB_PORT" "65263")' in setup
    assert '/tmp/virtuoso_bridge_designer/90590/virtuoso_bridge_t28_digital/ramic_bridge.il' in setup


def test_status_infers_profile_scoped_setup_path(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    monkeypatch.delenv("VB_CLIENT_ID_t28_io", raising=False)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    monkeypatch.setattr(cli, "_CLI_PROFILE", ["t28_io"])
    monkeypatch.setenv("VB_REMOTE_HOST_t28_io", "thu-wei")
    monkeypatch.setenv("VB_REMOTE_USER_t28_io", "designer")

    class _FakeSSHClient:
        @staticmethod
        def read_state(profile=None):
            return None

        @staticmethod
        def is_running(profile=None):
            return False

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)

    rc = cli._print_status()

    assert rc == 1
    assert (
        'load("/tmp/virtuoso_bridge_designer/90590/virtuoso_bridge_t28_io/virtuoso_setup.il")'
        in capsys.readouterr().out
    )


def test_status_no_response_prints_stale_daemon_hint(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_print_spectre_status", lambda profile, suffix: None)
    monkeypatch.setattr(cli, "_CLI_PROFILE", ["t28_io"])
    monkeypatch.setenv("VB_REMOTE_HOST_t28_io", "thu-wei")
    monkeypatch.setenv("VB_REMOTE_USER_t28_io", "designer")

    class _FakeSSHClient:
        @staticmethod
        def read_state(profile=None):
            return {
                "port": 65271,
                "setup_path": "/tmp/virtuoso_bridge_designer/virtuoso_bridge_t28_io/virtuoso_setup.il",
            }

        @staticmethod
        def is_running(profile=None):
            return True

    class _FakeVirtuosoClient:
        def __init__(self, host, port, timeout):
            pass

        def test_connection(self, timeout=5):
            return False

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.VirtuosoClient", _FakeVirtuosoClient)

    rc = cli._print_status()

    out = capsys.readouterr().out
    assert rc == 0
    assert "[daemon] NO RESPONSE" in out
    assert "load() did not replace the existing daemon" in out
    assert "RBStop()" in out
    assert "RBStopAll()" in out


def test_status_fails_when_daemon_user_differs_from_tunnel_user(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_print_spectre_status", lambda profile, suffix: None)
    monkeypatch.setattr(cli, "_CLI_PROFILE", ["t28_io"])
    monkeypatch.setenv("VB_REMOTE_HOST_t28_io", "thu-wei")
    monkeypatch.setenv("VB_REMOTE_USER_t28_io", "designer")
    monkeypatch.delenv("VB_ALLOW_CROSS_USER_DAEMON", raising=False)

    class _FakeSSHClient:
        @staticmethod
        def read_state(profile=None):
            return {"port": 65271, "setup_path": "/tmp/virtuoso_setup.il"}

        @staticmethod
        def is_running(profile=None):
            return True

    class _FakeVirtuosoClient:
        def __init__(self, host, port, timeout):
            pass

        def test_connection(self, timeout=5):
            return True

        def execute_skill(self, expr, timeout=5):
            values = {
                'getShellEnvVar("USER")': "other_user",
                "getHostName()": "thu-wei",
                "getCurrentTime()": "now",
                "getVersion()": "ICADVM",
                "getWorkingDir()": "/home/other_user/TSMC28",
            }
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=values.get(expr, ""))

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.VirtuosoClient", _FakeVirtuosoClient)

    rc = cli._print_status()

    out = capsys.readouterr().out
    assert rc == 1
    assert "[daemon identity] FAILED" in out
    assert "other_user" in out
    assert "designer" in out


def test_status_allows_cross_user_with_explicit_override(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_print_spectre_status", lambda profile, suffix: None)
    monkeypatch.setattr(cli, "_CLI_PROFILE", ["t28_io"])
    monkeypatch.setenv("VB_REMOTE_HOST_t28_io", "thu-wei")
    monkeypatch.setenv("VB_REMOTE_USER_t28_io", "designer")
    monkeypatch.setenv("VB_ALLOW_CROSS_USER_DAEMON", "1")

    class _FakeSSHClient:
        @staticmethod
        def read_state(profile=None):
            return {"port": 65271, "setup_path": "/tmp/virtuoso_setup.il"}

        @staticmethod
        def is_running(profile=None):
            return True

    class _FakeVirtuosoClient:
        def __init__(self, host, port, timeout):
            pass

        def test_connection(self, timeout=5):
            return True

        def execute_skill(self, expr, timeout=5):
            values = {
                'getShellEnvVar("USER")': "other_user",
                "getHostName()": "thu-wei",
                "getCurrentTime()": "now",
                "getVersion()": "ICADVM",
                "getWorkingDir()": "/home/other_user/TSMC28",
            }
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=values.get(expr, ""))

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.VirtuosoClient", _FakeVirtuosoClient)

    rc = cli._print_status()

    out = capsys.readouterr().out
    assert rc == 0
    assert "[daemon identity] FAILED" not in out

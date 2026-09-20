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
    assert 'setShellEnvVar("RB_IDENTITY_PATH"' in setup
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


def test_status_no_response_prints_stale_daemon_hint(monkeypatch, capsys, tmp_path) -> None:
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

        daemon_token = None

        def execute_skill(self, skill, timeout=5):
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["Empty response from daemon"],
            )

        def test_connection(self, timeout=5):
            return False

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.VirtuosoClient", _FakeVirtuosoClient)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    rc = cli._print_status()

    out = capsys.readouterr().out
    assert rc == 0
    assert "[daemon] NO RESPONSE" in out
    assert "load() did not replace the existing daemon" in out
    assert "RBStop()" in out
    assert "RBStopAll()" in out


def test_restart_daemon_loads_current_setup_and_accepts_disconnect(monkeypatch, capsys) -> None:
    class _FakeSSHClient:
        @staticmethod
        def read_state(profile=None):
            assert profile == "t28_io"
            return {
                "port": 65271,
                "setup_path": '/tmp/bridge path/virtuoso"setup.il',
            }

    class _FakeVirtuosoClient:
        instances: list["_FakeVirtuosoClient"] = []

        def __init__(self, host, port, timeout, log_to_ciw=True):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.log_to_ciw = log_to_ciw
            self.skill: str | None = None
            _FakeVirtuosoClient.instances.append(self)

        def execute_skill(self, skill: str, timeout=5):
            self.skill = skill
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["Empty response from daemon"],
            )

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.VirtuosoClient", _FakeVirtuosoClient)
    monkeypatch.setattr(
        "virtuoso_bridge.daemon_guard.check_daemon_user",
        lambda client, profile, timeout=5: type("Check", (), {"ok": True, "error": ""})(),
    )

    cli._restart_daemon_one("t28_io")

    out = capsys.readouterr().out
    client = _FakeVirtuosoClient.instances[0]
    assert client.host == "127.0.0.1"
    assert client.port == 65271
    assert client.log_to_ciw is False
    assert client.skill == 'RBStop()\nload("/tmp/bridge path/virtuoso\\"setup.il")'
    assert "Restarting daemon [t28_io]" in out
    assert "old daemon closed the connection while restarting" in out


def test_restart_daemon_refuses_cross_user_daemon(monkeypatch, capsys) -> None:
    class _FakeSSHClient:
        @staticmethod
        def read_state(profile=None):
            return {"port": 65271, "setup_path": "/tmp/virtuoso_setup.il"}

    class _FakeVirtuosoClient:
        def __init__(self, host, port, timeout, log_to_ciw=True):
            pass

        def execute_skill(self, skill: str, timeout=5):
            raise AssertionError("restart must not be sent after identity mismatch")

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.VirtuosoClient", _FakeVirtuosoClient)
    monkeypatch.setattr(
        "virtuoso_bridge.daemon_guard.check_daemon_user",
        lambda client, profile, timeout=5: type(
            "Check",
            (),
            {"ok": False, "error": "daemon Unix user 'alice' does not match configured VB_REMOTE_USER 'bob'"},
        )(),
    )

    cli._restart_daemon_one(None)

    out = capsys.readouterr().out
    assert "Refusing to restart daemon" in out
    assert "alice" in out
    assert "bob" in out


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


def test_status_diagnoses_banner_host_when_tunnel_endpoint_is_wrong(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_print_spectre_status", lambda profile, suffix: None)
    monkeypatch.setattr(cli, "_CLI_PROFILE", ["split"])
    monkeypatch.setenv("VB_GUI_HOST_split", "gui-a")
    monkeypatch.setenv("VB_DEPLOY_HOST_split", "gui-a")
    monkeypatch.setenv("VB_DAEMON_HOST_split", "gui-a")
    monkeypatch.setenv("VB_REMOTE_USER_split", "designer")

    class _FakeSSHClient:
        @staticmethod
        def read_state(profile=None):
            return {
                "port": 65271,
                "setup_path": "/shared/virtuoso_setup.il",
                "daemon_endpoint_hostname": "gui-a.example.edu",
            }

        @staticmethod
        def is_running(profile=None):
            return True

        @classmethod
        def from_env(cls, **_kwargs):
            return cls()

        def read_daemon_identity(self):
            return {"host": "compute-b", "ip": "192.0.2.20"}

        def probe_daemon_endpoint_hostname(self):
            return "gui-a.example.edu"

        def close(self):
            pass

    class _FakeVirtuosoClient:
        def __init__(self, host, port, timeout):
            pass

        daemon_token = None

        def execute_skill(self, skill, timeout=5):
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["Empty response from daemon"],
            )

        def test_connection(self, timeout=5):
            return False

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.VirtuosoClient", _FakeVirtuosoClient)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    rc = cli._print_status()

    out = capsys.readouterr().out
    assert rc == 0
    assert "[daemon host]" in out
    assert "compute-b" in out
    assert "gui-a.example.edu" in out
    assert "VB_DAEMON_HOST_split=compute-b" in out


# ---------------------------------------------------------------------------
# Remote port de-confliction (cross-user port squatting)
# ---------------------------------------------------------------------------

from virtuoso_bridge.transport.tunnel import (  # noqa: E402
    _remote_port_occupancy_cmd,
    _update_env_file,
)


class _ScriptedRunner:
    """FakeRunner whose occupancy probes replay scripted verdicts."""

    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts = list(verdicts)
        self.commands: list[str] = []
        self.uploads: dict[str, str] = {}

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        if "echo OWN" in command:
            verdict = self.verdicts.pop(0) if self.verdicts else "FREE"
            return CommandResult(returncode=0, stdout=f"{verdict}\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    def upload_text(self, text: str, remote_path: str, timeout=None) -> CommandResult:
        self.uploads[remote_path] = text
        return CommandResult(returncode=0, stdout="", stderr="")


def _make_remote_client(port: int) -> SSHClient:
    client = SSHClient(
        remote_host="thu-wei",
        remote_user="designer",
        port=port,
        profile="t28_digital",
    )
    return client


def test_occupancy_cmd_targets_port() -> None:
    cmd = _remote_port_occupancy_cmd(65061)
    assert ":65061$" in cmd
    assert "pgrep" in cmd and "id -un" in cmd


def test_deconflict_shifts_off_foreign_listener(monkeypatch) -> None:
    client = _make_remote_client(65061)
    runner = _ScriptedRunner(["FOREIGN", "FOREIGN", "FREE"])
    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "virtuoso_bridge.transport.tunnel._update_env_file",
        lambda key, value: updates.append((key, value)) or True,
    )

    changed = client._deconflict_remote_port(runner)

    assert changed
    assert client._port == 65063
    assert updates == [("VB_REMOTE_PORT_t28_digital", "65063")]


def test_deconflict_keeps_free_and_own_ports(monkeypatch) -> None:
    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "virtuoso_bridge.transport.tunnel._update_env_file",
        lambda key, value: updates.append((key, value)) or True,
    )

    client = _make_remote_client(65061)
    assert not client._deconflict_remote_port(_ScriptedRunner(["FREE"]))
    assert client._port == 65061

    # A port held by this user's own bridge daemon stays ours.
    client = _make_remote_client(65061)
    assert not client._deconflict_remote_port(_ScriptedRunner(["OWN"]))
    assert client._port == 65061
    assert updates == []


def test_deconflict_survives_probe_failure(monkeypatch) -> None:
    client = _make_remote_client(65061)

    class _BrokenRunner(_ScriptedRunner):
        def run_command(self, command: str, timeout=None) -> CommandResult:
            raise RuntimeError("ssh down")

    assert not client._deconflict_remote_port(_BrokenRunner([]))
    assert client._port == 65061


def test_deconflict_untouched_when_probe_output_unrecognized() -> None:
    client = _make_remote_client(65061)
    runner = _ScriptedRunner([])
    runner.verdicts = []  # probes answer FREE (empty script) — fine
    # A runner returning garbage stdout must keep the configured port.
    class _GarbageRunner(_ScriptedRunner):
        def run_command(self, command: str, timeout=None) -> CommandResult:
            return CommandResult(returncode=0, stdout="", stderr="")

    assert not client._deconflict_remote_port(_GarbageRunner([]))
    assert client._port == 65061


def test_deconflict_gives_up_when_range_exhausted() -> None:
    client = _make_remote_client(65061)
    runner = _ScriptedRunner(["FOREIGN"] * 100)
    assert not client._deconflict_remote_port(runner)
    assert client._port == 65061


def test_ensure_remote_setup_deploys_shifted_port(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    monkeypatch.delenv("VB_CLIENT_ID_t28_digital", raising=False)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    fake = _ScriptedRunner(["FOREIGN", "FREE"])
    client = _make_remote_client(65263)
    client._ssh_runner = fake
    monkeypatch.setattr(client, "_detect_remote_python", lambda: ("python3", 3, 11))
    monkeypatch.setattr(
        "virtuoso_bridge.transport.tunnel._update_env_file", lambda key, value: True
    )

    client.ensure_remote_setup()

    setup_path = "/tmp/virtuoso_bridge_designer/90590/virtuoso_bridge_t28_digital/virtuoso_setup.il"
    setup = fake.uploads[setup_path]
    # 65263 was FOREIGN -> setup must target the next free port, 65264.
    assert 'setShellEnvVar("RB_PORT" "65264")' in setup
    assert client._port == 65264

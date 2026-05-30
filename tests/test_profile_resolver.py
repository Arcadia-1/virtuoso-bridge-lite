from __future__ import annotations

from pathlib import Path
import sys

import pytest

from virtuoso_bridge import cli
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.profile import (
    bind_venv_profile,
    clear_venv_profile,
    resolve_profile,
    resolve_profile_info,
    venv_profile_path,
)
from virtuoso_bridge.spectre.runner import SpectreSimulator
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


def _isolate_profile_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VB_PROFILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "base-python"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base-python"))
    monkeypatch.setattr(
        "virtuoso_bridge.profile.default_user_env_path",
        lambda: tmp_path / "missing-user.env",
    )
    monkeypatch.setattr(
        "virtuoso_bridge.profile.get_runtime_env_file",
        lambda: None,
    )


def test_resolve_profile_prefers_explicit(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VB_PROFILE", "from_env")

    info = resolve_profile_info("explicit")

    assert info.profile == "explicit"
    assert info.source == "explicit"


def test_resolve_profile_prefers_environment_over_venv(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    venv = tmp_path / ".venv"
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    bind_venv_profile("from_venv")
    monkeypatch.setenv("VB_PROFILE", "from_env")

    info = resolve_profile_info()

    assert info.profile == "from_env"
    assert info.source == "environment"


def test_resolve_profile_reads_runtime_env_before_venv(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    runtime_env = tmp_path / "custom.env"
    runtime_env.write_text("VB_PROFILE=from_runtime\n", encoding="utf-8")
    monkeypatch.setattr(
        "virtuoso_bridge.profile.get_runtime_env_file",
        lambda: runtime_env,
    )
    venv = tmp_path / ".venv"
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    bind_venv_profile("from_venv")

    info = resolve_profile_info()

    assert info.profile == "from_runtime"
    assert info.source == "runtime_env"
    assert info.path == runtime_env


def test_resolve_profile_reads_venv_binding(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    venv = tmp_path / ".venv"
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    binding = bind_venv_profile("t180_io")

    info = resolve_profile_info()

    assert binding == venv / ".virtuoso-bridge-profile"
    assert info.profile == "t180_io"
    assert info.source == "venv"
    assert info.path == binding


def test_resolve_profile_reads_user_env_after_venv(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    user_env = tmp_path / "user.env"
    user_env.write_text("VB_PROFILE=user_default\n", encoding="utf-8")
    monkeypatch.setattr(
        "virtuoso_bridge.profile.default_user_env_path",
        lambda: user_env,
    )

    assert resolve_profile() == "user_default"


def test_resolve_profile_returns_none_without_binding(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)

    info = resolve_profile_info()

    assert info.profile is None
    assert info.source == "default"


def test_clear_venv_profile_removes_binding(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    path = bind_venv_profile("t28_io")

    cleared = clear_venv_profile()

    assert cleared == path
    assert not path.exists()
    assert venv_profile_path() == path


def test_venv_profile_path_falls_back_to_sys_prefix(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    venv = tmp_path / ".venv"
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base-python"))

    assert venv_profile_path() == venv / ".virtuoso-bridge-profile"


def test_virtuoso_client_from_env_uses_resolved_profile(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    bind_venv_profile("t28_digital")
    monkeypatch.setattr(
        "virtuoso_bridge.virtuoso.basic.bridge.load_vb_env",
        lambda: None,
    )
    seen: list[tuple[str, str | None]] = []

    class _FakeSSHClient:
        @staticmethod
        def is_running(profile=None):
            seen.append(("is_running", profile))
            return True

        @staticmethod
        def read_state(profile=None):
            seen.append(("read_state", profile))
            return {"port": 65501}

        @classmethod
        def from_env(cls, keep_remote_files=True, profile=None):
            seen.append(("from_env", profile))
            return cls()

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)

    client = VirtuosoClient.from_env()

    assert client.port == 65501
    assert seen == [
        ("is_running", "t28_digital"),
        ("read_state", "t28_digital"),
        ("from_env", "t28_digital"),
    ]


def test_virtuoso_client_from_env_rejects_cross_user_daemon(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VB_REMOTE_HOST_t28_io", "thu-wei")
    monkeypatch.setenv("VB_REMOTE_USER_t28_io", "designer")
    monkeypatch.delenv("VB_ALLOW_CROSS_USER_DAEMON", raising=False)
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.load_vb_env", lambda: None)

    class _FakeSSHClient:
        @staticmethod
        def is_running(profile=None):
            return True

        @staticmethod
        def read_state(profile=None):
            return {"port": 65271}

        @classmethod
        def from_env(cls, keep_remote_files=True, profile=None):
            return cls()

    def fake_execute_skill(self, expr, timeout=None):
        assert expr == 'getShellEnvVar("USER")'
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="other_user")

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)
    monkeypatch.setattr(VirtuosoClient, "execute_skill", fake_execute_skill)

    with pytest.raises(RuntimeError, match="daemon Unix user"):
        VirtuosoClient.from_env(profile="t28_io")


def test_virtuoso_client_from_env_allows_cross_user_override(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VB_REMOTE_HOST_t28_io", "thu-wei")
    monkeypatch.setenv("VB_REMOTE_USER_t28_io", "designer")
    monkeypatch.setenv("VB_ALLOW_CROSS_USER_DAEMON", "1")
    monkeypatch.setattr("virtuoso_bridge.virtuoso.basic.bridge.load_vb_env", lambda: None)

    class _FakeSSHClient:
        @staticmethod
        def is_running(profile=None):
            return True

        @staticmethod
        def read_state(profile=None):
            return {"port": 65271}

        @classmethod
        def from_env(cls, keep_remote_files=True, profile=None):
            return cls()

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)

    client = VirtuosoClient.from_env(profile="t28_io")

    assert client.port == 65271


def test_spectre_simulator_direct_constructor_uses_resolved_profile(monkeypatch, tmp_path) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    bind_venv_profile("t28_io")
    monkeypatch.setenv("VB_REMOTE_HOST_t28_io", "thu-wei")
    monkeypatch.setenv("VB_REMOTE_USER_t28_io", "designer")
    monkeypatch.setattr("virtuoso_bridge.spectre.runner.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)

    sim = SpectreSimulator(remote=True)

    assert sim._profile == "t28_io"
    assert sim._remote_host == "thu-wei"
    assert sim._remote_user == "designer"


def test_cli_profile_bind_show_clear(monkeypatch, tmp_path, capsys) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))

    assert cli.main(["profile", "bind", "t180_io", "--venv"]) == 0
    assert (tmp_path / ".venv" / ".virtuoso-bridge-profile").read_text(encoding="utf-8") == "t180_io\n"

    assert cli.main(["profile", "show"]) == 0
    out = capsys.readouterr().out
    assert "resolved profile : t180_io" in out
    assert "source           : venv" in out

    assert cli.main(["profile", "clear", "--venv"]) == 0
    assert not (tmp_path / ".venv" / ".virtuoso-bridge-profile").exists()


def test_cli_status_uses_venv_binding(monkeypatch, tmp_path, capsys) -> None:
    _isolate_profile_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    bind_venv_profile("t28_io")
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_HOST_t28_io", "thu-wei")
    monkeypatch.setenv("VB_REMOTE_USER_t28_io", "designer")

    class _FakeSSHClient:
        @staticmethod
        def read_state(profile=None):
            assert profile == "t28_io"
            return None

        @staticmethod
        def is_running(profile=None):
            assert profile == "t28_io"
            return False

    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)

    rc = cli.main(["status"])

    assert rc == 1
    assert "[t28_io]" in capsys.readouterr().out

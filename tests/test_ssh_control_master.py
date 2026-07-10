from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from virtuoso_bridge.transport.ssh import CommandResult, SSHRunner
from virtuoso_bridge.transport.tunnel import SSHClient


class _Pipe:
    def close(self) -> None:
        pass


class _Stderr:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


def _tar_extract_root(cmd: list[str], cwd: Path | None) -> Path:
    if "-C" not in cmd:
        assert cwd is not None
        return Path(cwd)
    extract_arg = Path(cmd[cmd.index("-C") + 1])
    if extract_arg.is_absolute():
        return extract_arg
    assert cwd is not None
    return Path(cwd) / extract_arg


def _install_download_pipeline(
    monkeypatch,
    *,
    ssh_exit_seconds: int = 0,
    reject_tar_path: Callable[[Path, bool], str | None] | None = None,
) -> None:
    class _FakeProcess:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.cwd = kwargs.get("cwd")
            self.is_tar = Path(cmd[0]).stem.lower() == "tar"
            self.returncode = 0
            self.stdout = _Pipe()
            self.stderr = _Stderr()

        def communicate(self, timeout=None):
            if not self.is_tar:
                return b"", b""
            extract_root = _tar_extract_root(self.cmd, self.cwd)
            rejection = (
                reject_tar_path(extract_root, "-C" in self.cmd)
                if reject_tar_path
                else None
            )
            if rejection:
                self.returncode = 2
                return b"", rejection.encode("utf-8")
            extracted = extract_root / "netlist"
            extracted.mkdir()
            (extracted / "input.scs").write_text("new netlist\n", encoding="utf-8")
            return b"", b""

        def wait(self, timeout=None):
            if not self.is_tar and timeout is not None and timeout < ssh_exit_seconds:
                raise subprocess.TimeoutExpired(self.cmd, timeout)
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("virtuoso_bridge.transport.ssh.subprocess.Popen", _FakeProcess)


def test_control_path_is_bounded_for_long_remote_identity(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)
    monkeypatch.delenv("VB_DISABLE_CONTROL_MASTER", raising=False)
    monkeypatch.delenv("VB_FORCE_CONTROL_MASTER", raising=False)

    runner = SSHRunner(
        host="srv-elamien.ece.mcmaster.ca",
        user="noorizad",
    )

    control_path = runner._control_path
    assert Path(control_path).name.startswith("vb_ssh_")
    assert "srv-elamien" not in control_path
    assert "noorizad" not in control_path
    if os.name != "nt":
        assert len(control_path) + 18 < 104


def test_macos_unix_listener_path_too_long_is_controlmaster_failure() -> None:
    stderr = (
        'unix_listener: path "/var/folders/x/y/T/'
        'vb_ssh_noorizad@srv-elamien.ece.mcmaster.ca_direct.2BclHvp4raDoiKZF" '
        "too long for Unix domain socket"
    )

    assert SSHRunner._is_cm_failure(255, stderr)
    assert not SSHRunner._is_cm_failure(0, stderr)


def test_remote_python_detection_error_includes_ssh_stderr() -> None:
    class _FakeRunner:
        def run_command(self, command: str, timeout=None) -> CommandResult:
            return CommandResult(
                returncode=255,
                stdout="",
                stderr='unix_listener: path "/var/folders/.../vb_ssh" too long for Unix domain socket',
            )

    client = SSHClient(remote_host="localhost")
    client._ssh_runner = _FakeRunner()

    with pytest.raises(RuntimeError) as excinfo:
        client._detect_remote_python()

    msg = str(excinfo.value)
    assert "No Python interpreter found on localhost" in msg
    assert "unix_listener" in msg
    assert "SSH return code: 255" in msg


def test_recursive_download_allows_ssh_exit_within_caller_timeout(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)
    _install_download_pipeline(monkeypatch, ssh_exit_seconds=6)

    local_path = tmp_path / "netlist"
    runner = SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh", timeout=30)

    result = runner.download("/remote/netlist", local_path, recursive=True)

    assert result.returncode == 0
    assert (local_path / "input.scs").read_text(encoding="utf-8") == "new netlist\n"


def test_recursive_download_supports_paths_outside_active_codepage(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)

    def reject_non_cp936(path: Path, path_from_argv: bool) -> str | None:
        if not path_from_argv:
            return None
        try:
            str(path).encode("cp936")
        except UnicodeEncodeError:
            return f"tar could not chdir to {path}"
        return None

    _install_download_pipeline(monkeypatch, reject_tar_path=reject_non_cp936)
    local_path = (
        tmp_path
        / "Chinese_\U00020bb7_emoji_\U0001f600"
        / "netlist_\U00020bb7_\U0001f600"
    )
    runner = SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh", timeout=30)

    result = runner.download("/remote/netlist", local_path, recursive=True)

    assert result.returncode == 0
    assert (local_path / "input.scs").read_text(encoding="utf-8") == "new netlist\n"


def test_recursive_download_uses_bounded_staging_for_long_target_name(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)

    local_parent = tmp_path / "long_target"
    target_name_length = 250 - len(str(local_parent)) - 1
    assert 1 <= target_name_length <= 240
    local_path = local_parent / ("n" * target_name_length)
    legacy_stage = local_path.with_name(f".{local_path.name}.tmp-" + ("0" * 32))
    assert len(str(local_path)) == 250
    assert len(str(legacy_stage)) >= 260

    def reject_legacy_long_path(path: Path, path_from_argv: bool) -> str | None:
        if path_from_argv and len(str(path)) >= 260:
            return f"tar could not chdir to {path}"
        return None

    _install_download_pipeline(monkeypatch, reject_tar_path=reject_legacy_long_path)
    runner = SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh", timeout=30)

    result = runner.download("/remote/netlist", local_path, recursive=True)

    assert result.returncode == 0
    assert (local_path / "input.scs").read_text(encoding="utf-8") == "new netlist\n"


def test_recursive_download_quotes_remote_path_components(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)

    commands: list[list[str]] = []
    process_cwds: list[Path | None] = []

    class _Pipe:
        def close(self) -> None:
            pass

    class _Stderr:
        def read(self) -> bytes:
            return b""

    class _FakeProcess:
        def __init__(self, cmd, **kwargs):
            commands.append(cmd)
            process_cwds.append(kwargs.get("cwd"))
            self.cmd = cmd
            self.cwd = kwargs.get("cwd")
            self.returncode = 0
            self.stdout = _Pipe()
            self.stderr = _Stderr()

        def communicate(self, timeout=None):
            if Path(self.cmd[0]).stem.lower() == "tar":
                (_tar_extract_root(self.cmd, self.cwd) / "netlist dir").mkdir()
            return b"", b""

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("virtuoso_bridge.transport.ssh.subprocess.Popen", _FakeProcess)

    runner = SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh", timeout=30)
    remote_path = "/remote/sim's results/netlist dir"
    result = runner.download(
        remote_path,
        tmp_path / "netlist dir",
        recursive=True,
    )

    assert result.returncode == 0
    remote_cmd = commands[0][-1]
    inner_cmd = shlex.split(remote_cmd)[2]
    assert f"p={shlex.quote(remote_path)}" in inner_cmd
    assert 'd=$(dirname "$p")' in inner_cmd
    assert 'b=$(basename "$p")' in inner_cmd
    assert 'cd "$d"' in inner_cmd
    assert 'tar czf - "$b"' in inner_cmd
    assert commands[1] == [runner._tar_cmd, "xzf", "-"]
    extract_dir = Path(process_cwds[1])
    assert extract_dir.parent == tmp_path
    assert extract_dir.name.startswith(".vbtmp-")


def test_recursive_download_extracts_into_requested_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)

    commands: list[list[str]] = []
    process_cwds: list[Path | None] = []

    class _Pipe:
        def close(self) -> None:
            pass

    class _Stderr:
        def read(self) -> bytes:
            return b""

    class _FakeProcess:
        def __init__(self, cmd, **kwargs):
            commands.append(cmd)
            process_cwds.append(kwargs.get("cwd"))
            self.cmd = cmd
            self.cwd = kwargs.get("cwd")
            self.returncode = 0
            self.stdout = _Pipe()
            self.stderr = _Stderr()

        def communicate(self, timeout=None):
            if Path(self.cmd[0]).stem.lower() == "tar":
                extracted = _tar_extract_root(self.cmd, self.cwd) / "netlist"
                extracted.mkdir()
                (extracted / "input.scs").write_text("new netlist\n", encoding="utf-8")
            return b"", b""

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("virtuoso_bridge.transport.ssh.subprocess.Popen", _FakeProcess)

    runner = SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh", timeout=30)
    local_path = tmp_path / ".netlist.tmp-123"
    result = runner.download(
        "/remote/simulation/netlist",
        local_path,
        recursive=True,
    )

    assert result.returncode == 0
    remote_cmd = commands[0][-1]
    inner_cmd = shlex.split(remote_cmd)[2]
    assert 'cd "$d"' in inner_cmd
    assert 'tar czf - "$b"' in inner_cmd
    assert commands[1] == [runner._tar_cmd, "xzf", "-"]
    extract_dir = Path(process_cwds[1])
    assert extract_dir.parent == tmp_path
    assert extract_dir.name.startswith(".vbtmp-")
    assert (local_path / "input.scs").read_text(encoding="utf-8") == "new netlist\n"


def test_recursive_download_preserves_existing_target_when_tar_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)

    class _Pipe:
        def close(self) -> None:
            pass

    class _Stderr:
        def read(self) -> bytes:
            return b"remote ok"

    class _FakeProcess:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.stdout = _Pipe()
            self.stderr = _Stderr()
            self.returncode = 0 if "ssh" in cmd[0] else 2

        def communicate(self, timeout=None):
            return b"", b"tar failed"

        def wait(self, timeout=None):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("virtuoso_bridge.transport.ssh.subprocess.Popen", _FakeProcess)

    existing = tmp_path / "netlist"
    existing.mkdir()
    (existing / "input.scs").write_text("old netlist\n", encoding="utf-8")
    runner = SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh", timeout=30)

    result = runner.download("/remote/netlist", existing, recursive=True)

    assert result.returncode != 0
    assert (existing / "input.scs").read_text(encoding="utf-8") == "old netlist\n"


def test_recursive_download_restores_existing_target_when_install_rename_fails(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)

    class _Pipe:
        def close(self) -> None:
            pass

    class _Stderr:
        def read(self) -> bytes:
            return b""

    class _FakeProcess:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.cwd = kwargs.get("cwd")
            self.stdout = _Pipe()
            self.stderr = _Stderr()
            self.returncode = 0

        def communicate(self, timeout=None):
            if Path(self.cmd[0]).stem.lower() == "tar":
                extracted = _tar_extract_root(self.cmd, self.cwd) / "netlist"
                extracted.mkdir()
                (extracted / "input.scs").write_text("new netlist\n", encoding="utf-8")
            return b"", b""

        def wait(self, timeout=None):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    original_rename = Path.rename

    def fail_temp_install(self: Path, target: Path):
        if self.name == "netlist" and self.parent.name.startswith(".vbtmp-"):
            raise OSError("install failed")
        return original_rename(self, target)

    monkeypatch.setattr("virtuoso_bridge.transport.ssh.subprocess.Popen", _FakeProcess)
    monkeypatch.setattr(Path, "rename", fail_temp_install)

    existing = tmp_path / "netlist"
    existing.mkdir()
    (existing / "input.scs").write_text("old netlist\n", encoding="utf-8")
    runner = SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh", timeout=30)

    with pytest.raises(OSError, match="install failed"):
        runner.download("/remote/netlist", existing, recursive=True)

    assert existing.is_dir()
    assert (existing / "input.scs").read_text(encoding="utf-8") == "old netlist\n"

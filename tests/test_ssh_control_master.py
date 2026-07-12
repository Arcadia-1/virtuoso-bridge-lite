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


def test_remote_scp_target_quotes_legacy_shell_metacharacters() -> None:
    runner = SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh")
    remote_path = "/scratch root/$(touch PWNED);'quoted'[1].gds"
    escaped_path = (
        r"/scratch\ root/\$\(touch\ PWNED\)\;\'quoted\'\[1\].gds"
    )

    target = runner._remote_scp_target(remote_path)
    host, separator, quoted_path = target.partition(":")

    assert host == "designer@eda-host"
    assert separator == ":"
    assert quoted_path == escaped_path
    assert shlex.split(quoted_path) == [remote_path]


def test_remote_scp_target_preserves_simple_paths_and_rejects_controls() -> None:
    runner = SSHRunner(host="eda-host", user=None, ssh_cmd="ssh")

    assert runner._remote_scp_target("/tmp/result.gds") == (
        "eda-host:/tmp/result.gds"
    )
    for remote_path in (
        "/tmp/bad\x00name",
        "/tmp/bad\tname",
        "/tmp/bad\nname",
        "/tmp/bad\rname",
        "/tmp/bad\x7fname",
    ):
        with pytest.raises(ValueError, match="remote SCP path"):
            runner._remote_scp_target(remote_path)


@pytest.mark.parametrize(
    ("remote_path", "escaped_path"),
    [
        ("/tmp/result.gds", "/tmp/result.gds"),
        (
            r"/scratch root/result\copy[1].gds",
            r"/scratch\ root/result\\copy\[1\].gds",
        ),
    ],
)
def test_nonrecursive_download_selects_safe_scp_mode(
    monkeypatch,
    tmp_path: Path,
    remote_path: str,
    escaped_path: str,
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(
        "virtuoso_bridge.transport.ssh.subprocess.run",
        fake_run,
    )
    runner = SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh")

    result = runner.download(remote_path, tmp_path / "result.gds")

    assert result.returncode == 0
    assert "-O" not in commands[0]
    target = commands[0][-2]
    assert target == f"designer@eda-host:{escaped_path}"
    assert shlex.split(target.partition(":")[2]) == [remote_path]


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


class _DeadlineClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def consume(
        self,
        duration: float,
        timeout: float,
        command: object,
    ) -> None:
        if timeout < duration:
            self.now += timeout
            raise subprocess.TimeoutExpired(command, timeout)
        self.now += duration


def _configure_deadline_runner(monkeypatch, clock: _DeadlineClock) -> SSHRunner:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.time.monotonic", clock)
    return SSHRunner(host="eda-host", user="designer", ssh_cmd="ssh")


@pytest.mark.parametrize(
    "operation",
    ["run_command", "scp_download", "upload_text"],
)
def test_retrying_public_calls_share_one_timeout_budget(
    monkeypatch,
    tmp_path: Path,
    operation: str,
) -> None:
    clock = _DeadlineClock()
    runner = _configure_deadline_runner(monkeypatch, clock)
    attempt_timeouts: list[float] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        attempt_timeout = kwargs["timeout"]
        attempt_timeouts.append(attempt_timeout)
        clock.consume(0.04, attempt_timeout, command)
        return subprocess.CompletedProcess(
            command,
            255,
            b"",
            b"Connection reset by peer",
        )

    monkeypatch.setattr(
        "virtuoso_bridge.transport.ssh.subprocess.run",
        fake_run,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        if operation == "run_command":
            runner.run_command("printf ok", timeout=0.05)
        elif operation == "scp_download":
            runner.download(
                "/remote/result.gds",
                tmp_path / "result.gds",
                timeout=0.05,
            )
        else:
            runner.upload_text("payload", "/remote/input.txt", timeout=0.05)

    assert attempt_timeouts == pytest.approx([0.05, 0.01])
    assert clock.now == pytest.approx(0.05)


def test_scp_download_cm_fallback_uses_remaining_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clock = _DeadlineClock()
    runner = _configure_deadline_runner(monkeypatch, clock)
    attempt_timeouts: list[float] = []
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        attempt_timeout = kwargs["timeout"]
        attempt_timeouts.append(attempt_timeout)
        commands.append(command)
        if len(commands) == 1:
            clock.consume(0.02, attempt_timeout, command)
            return subprocess.CompletedProcess(
                command,
                255,
                b"",
                b"mux_client_request_session: master session id: 2",
            )
        clock.consume(0.01, attempt_timeout, command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(
        "virtuoso_bridge.transport.ssh.subprocess.run",
        fake_run,
    )

    result = runner.download(
        "/remote/result.gds",
        tmp_path / "result.gds",
        timeout=0.05,
    )

    assert result.returncode == 0
    assert attempt_timeouts == pytest.approx([0.05, 0.03])
    assert any("ControlMaster=auto" in part for part in commands[0])
    assert not any("ControlMaster=auto" in part for part in commands[1])
    assert runner._use_control_master is False


def test_tar_upload_retries_share_one_timeout_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clock = _DeadlineClock()
    runner = _configure_deadline_runner(monkeypatch, clock)
    communicate_timeouts: list[float] = []

    class _UploadProcess:
        def __init__(self, command: list[str], **_kwargs) -> None:
            self.command = command
            self.is_tar = Path(command[0]).stem.lower() == "tar"
            self.returncode = 0 if self.is_tar else 255
            self.stdout = _Pipe()
            self.stderr = _Stderr(b"Connection reset by peer")

        def communicate(self, timeout=None):
            assert timeout is not None
            communicate_timeouts.append(timeout)
            clock.consume(0.04, timeout, self.command)
            return b"", b"Connection reset by peer"

        def wait(self, timeout=None):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(
        "virtuoso_bridge.transport.ssh.subprocess.Popen",
        _UploadProcess,
    )
    local_path = tmp_path / "input.txt"
    local_path.write_text("payload", encoding="utf-8")

    with pytest.raises(subprocess.TimeoutExpired):
        runner.upload(local_path, "/remote/input.txt", timeout=0.05)

    assert communicate_timeouts == pytest.approx([0.05, 0.01])
    assert clock.now == pytest.approx(0.05)


def test_batch_upload_groups_share_one_timeout_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clock = _DeadlineClock()
    runner = _configure_deadline_runner(monkeypatch, clock)
    communicate_timeouts: list[float] = []

    class _BatchUploadProcess:
        def __init__(self, command: list[str], **_kwargs) -> None:
            self.command = command
            self.is_tar = Path(command[0]).stem.lower() == "tar"
            self.returncode = 0
            self.stdout = _Pipe()
            self.stderr = _Stderr()

        def communicate(self, timeout=None):
            assert not self.is_tar
            assert timeout is not None
            communicate_timeouts.append(timeout)
            clock.consume(0.04, timeout, self.command)
            return b"", b""

        def wait(self, timeout=None):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(
        "virtuoso_bridge.transport.ssh.subprocess.Popen",
        _BatchUploadProcess,
    )
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    with pytest.raises(subprocess.TimeoutExpired):
        runner.upload_batch(
            [
                (first, "/remote/a/first.txt"),
                (second, "/remote/b/second.txt"),
            ],
            timeout=0.05,
        )

    assert communicate_timeouts == pytest.approx([0.05, 0.01])
    assert clock.now == pytest.approx(0.05)


def test_recursive_download_pipeline_shares_one_timeout_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clock = _DeadlineClock()
    runner = _configure_deadline_runner(monkeypatch, clock)
    tar_timeouts: list[float] = []
    ssh_timeouts: list[float] = []

    class _DownloadProcess:
        def __init__(self, command: list[str], **kwargs) -> None:
            self.command = command
            self.cwd = kwargs.get("cwd")
            self.is_tar = Path(command[0]).stem.lower() == "tar"
            self.returncode = 0
            self.stdout = _Pipe()
            self.stderr = _Stderr()

        def communicate(self, timeout=None):
            assert self.is_tar
            assert timeout is not None
            tar_timeouts.append(timeout)
            clock.consume(0.04, timeout, self.command)
            extracted = Path(self.cwd) / "netlist"
            extracted.mkdir()
            return b"", b""

        def wait(self, timeout=None):
            assert not self.is_tar
            assert timeout is not None
            ssh_timeouts.append(timeout)
            clock.consume(0.04, timeout, self.command)
            return 0

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(
        "virtuoso_bridge.transport.ssh.subprocess.Popen",
        _DownloadProcess,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        runner.download(
            "/remote/netlist",
            tmp_path / "netlist",
            recursive=True,
            timeout=0.05,
        )

    assert tar_timeouts == pytest.approx([0.05])
    assert ssh_timeouts == pytest.approx([0.01])
    assert clock.now == pytest.approx(0.05)


def test_persistent_shell_probe_and_command_share_one_timeout_budget(
    monkeypatch,
) -> None:
    clock = _DeadlineClock()
    runner = _configure_deadline_runner(monkeypatch, clock)
    runner._persistent_shell_enabled = True
    command_timeouts: list[float] = []

    def fake_ensure(timeout=None, **kwargs) -> None:
        budget = kwargs.get("_budget")
        remaining = budget.remaining("persistent-shell probe") if budget else timeout
        assert remaining is not None
        clock.consume(0.04, remaining, "persistent-shell probe")

    def fake_command(command: str, timeout=None, **kwargs) -> CommandResult:
        budget = kwargs.get("_budget")
        remaining = budget.remaining(command) if budget else timeout
        assert remaining is not None
        command_timeouts.append(remaining)
        clock.consume(0.04, remaining, command)
        return CommandResult(0, "ok", "")

    monkeypatch.setattr(runner, "ensure_persistent_shell", fake_ensure)
    monkeypatch.setattr(
        runner,
        "_run_command_via_persistent_shell_locked",
        fake_command,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_command("printf ok", timeout=0.05)

    assert command_timeouts == pytest.approx([0.01])
    assert clock.now == pytest.approx(0.05)

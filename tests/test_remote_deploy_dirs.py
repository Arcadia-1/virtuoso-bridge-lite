"""The bridge deploy directory on a shared host must be private to the user.

The CIW loads code from it (and ``.cdsinit`` may autoload it), so another
user who pre-creates part of the path in a shared /tmp must not be able to
plant files there. The generated shell runs for real with the local POSIX
sh, against a temporary scratch root.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.transport.tunnel import SSHClient

pytestmark = pytest.mark.skipif(
    os.name == "nt" or not shutil.which("sh"), reason="requires a POSIX shell"
)


class _LocalShellRunner:
    """Runs commands with the local sh, as the SSH runner does on the host."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env
        self.uploads: dict[str, str] = {}

    def run_command(self, command: str, timeout=None) -> CommandResult:
        proc = subprocess.run(
            ["sh", "-s"], input=command, capture_output=True, text=True, timeout=30, env=self.env,
        )
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def upload_text(self, text: str, remote_path: str, timeout=None) -> CommandResult:
        self.uploads[remote_path] = text
        return CommandResult(returncode=0, stdout="", stderr="")


def _deploy(monkeypatch, root, runner) -> SSHClient:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", str(root))
    monkeypatch.setenv("VB_CLIENT_ID", "client1")
    client = SSHClient(remote_host="eda-host", remote_user="designer", port=65000)
    client._ssh_runner = runner
    client._deployment_runner = runner
    monkeypatch.setattr(client, "_detect_remote_python", lambda: ("python3", 3, 11))
    monkeypatch.setattr(client, "_deconflict_remote_port", lambda _runner: None)
    client.ensure_remote_setup()
    return client


def _chain(root):
    top = root / "virtuoso_bridge_designer"
    return [top, top / "client1", top / "client1" / "virtuoso_bridge"]


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_deploy_creates_every_directory_private(monkeypatch, tmp_path):
    runner = _LocalShellRunner()

    client = _deploy(monkeypatch, tmp_path, runner)

    assert client.remote_work_dir == str(_chain(tmp_path)[-1])
    assert [_mode(path) for path in _chain(tmp_path)] == [0o700] * 3
    assert f"{client.remote_work_dir}/virtuoso_setup.il" in runner.uploads


@pytest.mark.parametrize("dirname", [".virtuoso-bridge", "bridge scratch"])
def test_deploy_in_existing_home_scratch_root(monkeypatch, tmp_path, dirname):
    # Synthetic remote home; execute the real generated POSIX shell locally.
    root = tmp_path / "home" / "designer" / dirname
    root.mkdir(parents=True, mode=0o700)
    runner = _LocalShellRunner()

    client = _deploy(monkeypatch, root, runner)

    assert client.remote_work_dir == str(_chain(root)[-1])
    assert [_mode(path) for path in _chain(root)] == [0o700] * 3
    assert f"{client.remote_work_dir}/virtuoso_setup.il" in runner.uploads
    assert all(path.startswith(str(root) + "/") for path in runner.uploads)


def test_deploy_refuses_existing_nonprivate_directories(monkeypatch, tmp_path):
    for path in _chain(tmp_path):
        path.mkdir()
        path.chmod(0o755)

    runner = _LocalShellRunner()
    with pytest.raises(RuntimeError, match="mode 700"):
        _deploy(monkeypatch, tmp_path, runner)

    assert [_mode(path) for path in _chain(tmp_path)] == [0o755] * 3
    assert not runner.uploads


def test_deploy_refuses_a_symlinked_directory(monkeypatch, tmp_path):
    planted = tmp_path / "planted"
    planted.mkdir()
    _chain(tmp_path)[0].symlink_to(planted)
    runner = _LocalShellRunner()

    with pytest.raises(RuntimeError, match="Refusing to deploy.*VB_REMOTE_SCRATCH_ROOT"):
        _deploy(monkeypatch, tmp_path, runner)

    assert list(planted.iterdir()) == []
    assert runner.uploads == {}


def test_deploy_refuses_a_directory_owned_by_someone_else(monkeypatch, tmp_path):
    # Pretend the deploying user has a different uid than the directory owner.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_id = fake_bin / "id"
    fake_id.write_text(f"#!/bin/sh\necho {os.getuid() + 1}\n", encoding="utf-8")
    fake_id.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "client1").mkdir(mode=0o777)
    runner = _LocalShellRunner(env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"})

    with pytest.raises(RuntimeError, match="Refusing to deploy.*(wrong owner|must be owned by this user)"):
        _deploy(monkeypatch, scratch, runner)

    assert runner.uploads == {}
    assert not (scratch / "client1" / "virtuoso_bridge").exists()

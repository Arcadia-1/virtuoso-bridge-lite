"""Execute staging shells on Linux; never connect to a remote machine or GUI."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso import x11


@pytest.fixture
def staging(tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("requires real Linux ownership, symlinks and POSIX shell")
    tmp_path.chmod(0o700)
    monkeypatch.setattr(x11, "remote_scratch_root", lambda *a: str(tmp_path))
    source = tmp_path / "source.py"
    source.write_text("print('fixture')\n")
    destination = tmp_path / "private" / "helper.py"
    class Shell:
        def run_command(self, command, timeout=None):
            result = subprocess.run(["sh", "-c", command], capture_output=True, text=True,
                                    timeout=min(timeout or 10, 10))
            return CommandResult(result.returncode, result.stdout, result.stderr)
        def upload(self, source, target, timeout=None):
            shutil.copyfile(source, target)
            return CommandResult(0, "", "")
    return Shell(), source, destination


def test_real_private_staging_and_cached_content_verification(staging):
    runner, source, destination = staging
    x11._stage_helper(runner, source, str(destination))
    assert destination.read_bytes() == source.read_bytes()
    assert destination.stat().st_mode & 0o777 == 0o600
    x11._stage_helper(runner, source, str(destination), upload=False)
    destination.write_text("changed")
    with pytest.raises(RuntimeError):
        x11._stage_helper(runner, source, str(destination), upload=False)


@pytest.mark.parametrize("kind", ["0755", "0777", "directory-link", "file-link", "fifo"])
def test_real_unsafe_staging_refused(staging, kind):
    runner, source, destination = staging
    if kind == "directory-link":
        destination.parent.symlink_to(source.parent, target_is_directory=True)
    else:
        destination.parent.mkdir(mode=0o700)
        if kind in ("0755", "0777"):
            destination.parent.chmod(int(kind, 8))
        elif kind == "file-link":
            destination.symlink_to(source)
        else:
            os.mkfifo(destination)
    with pytest.raises(RuntimeError):
        x11._stage_helper(runner, source, str(destination), timeout=2)
    assert source.read_text() == "print('fixture')\n"


def test_real_upload_failure_does_not_install_helper(staging, monkeypatch):
    runner, source, destination = staging
    monkeypatch.setattr(runner, "upload", lambda *a, **kw: CommandResult(1, "", "upload failed"))
    with pytest.raises(RuntimeError, match="upload failed"):
        x11._stage_helper(runner, source, str(destination))
    assert not destination.exists()

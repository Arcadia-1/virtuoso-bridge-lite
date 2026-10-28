"""Real bounded Linux publication tests, independent of SSH and Cadence."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux flock and directory publication")


@pytest.fixture
def publication(tmp_path):
    from virtuoso_bridge.virtuoso.basic.resources import ramic_deploy
    return ramic_deploy, tmp_path / "daemon"


def test_different_bundle_requires_explicit_replacement(publication):
    module, path = publication
    old = {"setup.il": "loopback", "daemon.py": "old code"}
    module.publish(str(path), old)
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in path.iterdir()}
    assert module.publish(str(path), old)["reused"]
    with pytest.raises(RuntimeError, match="--replace-setup"):
        module.publish(str(path), {"setup.il": "listen all", "daemon.py": "new code"})
    assert {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in path.iterdir()} == before
    result = module.publish(str(path), {"setup.il": "listen all", "daemon.py": "new code"}, replace=True)
    assert (path / "setup.il").read_text() == "listen all"
    assert (Path(result["backup"]) / "setup.il").read_text() == "loopback"
    assert path.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in path.iterdir())


def test_failed_exchange_restores_prior_complete_bundle(publication, monkeypatch):
    module, path = publication
    module.publish(str(path), {"a": "old", "b": "old"})
    rename = module.os.rename
    def fail_stage(source, destination):
        if "-stage-" in source:
            raise OSError("fixture interrupted publication")
        return rename(source, destination)
    monkeypatch.setattr(module.os, "rename", fail_stage)
    with pytest.raises(OSError, match="interrupted"):
        module.publish(str(path), {"a": "new", "b": "new"}, replace=True)
    assert [p.read_text() for p in sorted(path.iterdir())] == ["old", "old"]
    assert not list(path.parent.glob(".daemon-stage-*"))


def test_crash_recovery_restores_complete_old_directory(publication):
    module, path = publication
    files = {"a": "old", "b": "old"}
    module.publish(str(path), files)
    recovery = path.parent / ".daemon-recover"
    path.rename(recovery)
    assert module.publish(str(path), files)["reused"]
    assert not recovery.exists()
    assert (path / "a").read_text() == "old"


def test_competing_publishers_never_mix_bundles(publication):
    module, path = publication
    def publish(number):
        content = str(number) * 1000
        return module.publish(str(path), {"a": content, "b": content}, replace=True)
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(publish, range(8)))
    assert (path / "a").read_text() == (path / "b").read_text()
    for result in results:
        if result.get("backup"):
            backup = Path(result["backup"])
            assert (backup / "a").read_text() == (backup / "b").read_text()


def test_publication_lock_timeout_is_bounded(publication):
    import fcntl
    import time
    module, path = publication
    lock = path.parent / ".daemon-deploy.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="Timed out"):
            module.publish(str(path), {"a": "value"}, timeout=0.1)
        assert time.monotonic() - started < 2
        assert not path.exists()
    finally:
        os.close(fd)


def test_symlink_member_never_followed_or_overwritten(publication):
    module, path = publication
    path.mkdir(mode=0o700)
    other = path.parent / "other"
    other.write_text("do not change")
    (path / "a").symlink_to(other)
    with pytest.raises(OSError):
        module.publish(str(path), {"a": "new"}, replace=True)
    assert other.read_text() == "do not change"


def test_remote_publication_runs_the_actual_shell_and_helper(tmp_path, monkeypatch, capsys):
    from tests.test_remote_deploy_dirs import _LocalShellRunner
    from virtuoso_bridge.transport import tunnel
    from virtuoso_bridge.transport.ssh import CommandResult
    class Runner(_LocalShellRunner):
        def upload_text(self, text, remote_path, timeout=None):
            fd = os.open(remote_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            return CommandResult(0, "", "")
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", str(tmp_path))
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    runner = Runner()
    def client(listen=False):
        value = tunnel.SSHClient("fixture", discover_instances=True, listen_all=listen)
        value._ssh_runner = runner
        monkeypatch.setattr(value, "_detect_remote_python", lambda: (sys.executable, 3, 13))
        return value
    client().ensure_remote_setup()
    path = tmp_path / "daemon" / "virtuoso_setup.il"
    old = path.read_text()
    differing = client(True)
    with pytest.raises(RuntimeError, match="--replace-setup"):
        differing.ensure_remote_setup()
    assert "listen on all interfaces" in capsys.readouterr().out
    assert path.read_text() == old
    differing.allow_setup_replacement()
    differing.ensure_remote_setup()
    assert 'RB_LISTEN_ALL" "1"' in path.read_text()
    assert not list(tmp_path.glob(".daemon-input-*"))

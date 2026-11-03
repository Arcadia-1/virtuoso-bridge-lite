"""Real-filesystem coverage for explicit local bundle migration."""
import os
from pathlib import Path
import sys

import pytest

from virtuoso_bridge.transport import tunnel


@pytest.fixture
def local_state(monkeypatch, tmp_path):
    monkeypatch.setattr(tunnel, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(tunnel.SSHClient, "_instance_root", lambda self: str(tmp_path / "registry"))
    return tmp_path


def snapshot(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir()}


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
def test_new_legacy_bundle_is_private(local_state):
    previous = os.umask(0o022)
    try:
        tunnel.SSHClient("localhost").ensure_local_setup()
    finally:
        os.umask(previous)
    assert (local_state / "local").stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform != "linux", reason="descriptor-anchored Linux publisher")
def test_local_migration_requires_explicit_replacement_and_keeps_backup(local_state):
    legacy = tunnel.SSHClient("localhost")
    legacy.ensure_local_setup()
    directory = Path(legacy.setup_path).parent
    before = snapshot(directory)
    automatic = tunnel.SSHClient("localhost", discover_instances=True)
    with pytest.raises(RuntimeError, match="--replace-setup"):
        automatic.ensure_local_setup()
    assert snapshot(directory) == before
    automatic.allow_setup_replacement()
    automatic.ensure_local_setup()
    backups = list(local_state.glob(".local-recover-*"))
    assert len(backups) == 1 and snapshot(backups[0]) == before
    assert 'setShellEnvVar("RB_AUTO_PORT" "1")' in Path(automatic.setup_path).read_text()


@pytest.mark.skipif(sys.platform != "linux", reason="descriptor-anchored Linux publisher")
def test_migration_does_not_chmod_existing_legacy_directory(local_state):
    directory = local_state / "local"
    directory.mkdir()
    directory.chmod(0o755)
    tunnel.SSHClient("localhost").ensure_local_setup()
    before = snapshot(directory)
    automatic = tunnel.SSHClient("localhost", discover_instances=True)
    automatic.allow_setup_replacement()
    with pytest.raises(ValueError, match="mode 0700"):
        automatic.ensure_local_setup()
    assert directory.stat().st_mode & 0o777 == 0o755
    assert snapshot(directory) == before
    assert not list(local_state.glob(".local-recover-*"))

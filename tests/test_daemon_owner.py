"""Process identity and deployment regressions for the shared owner helper."""

import subprocess
import sys

import pytest

from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.virtuoso.basic.resources import ramic_owner


@pytest.mark.parametrize("boot", [None, "", "foreign-boot"])
def test_foreign_or_unknown_boot_never_queries_or_signals_pid(monkeypatch, boot):
    monkeypatch.setattr(ramic_owner, "_boot_id", lambda: "local-boot")

    def forbidden(*args):
        pytest.fail("foreign owner accessed the local process table")

    monkeypatch.setattr(ramic_owner, "_identity", forbidden)
    monkeypatch.setattr(ramic_owner.os, "kill", forbidden)
    owner = ramic_owner.OwnerProcess(123, boot)
    owner.start_monitor()
    owner.interrupt()


@pytest.mark.parametrize("replacement", [None, "new-start-time"])
def test_dead_or_recycled_owner_is_not_signalled(monkeypatch, replacement):
    monkeypatch.setattr(ramic_owner, "_boot_id", lambda: "local-boot")
    monkeypatch.setattr(ramic_owner, "_identity", lambda pid: "original-start-time")
    owner = ramic_owner.OwnerProcess(123, "local-boot")
    signals = []
    monkeypatch.setattr(ramic_owner.os, "kill", lambda *args: signals.append(args))
    owner.interrupt()
    assert len(signals) == 1
    monkeypatch.setattr(ramic_owner, "_identity", lambda pid: replacement)
    assert not owner.alive()
    owner.interrupt()
    assert len(signals) == 1
    with pytest.raises(SystemExit) as stopped:
        owner.start_monitor()
    assert stopped.value.code == 0


def test_local_setup_deploys_importable_owner_helper(monkeypatch, tmp_path):
    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.state_dir", lambda: tmp_path)
    client = SSHClient(remote_host="localhost")
    client.ensure_local_setup()
    result = subprocess.run(
        [sys.executable, "-c", "from ramic_owner import OwnerProcess; print(OwnerProcess.__name__)"],
        cwd=tmp_path / "local", capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OwnerProcess"

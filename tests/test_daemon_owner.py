"""Process identity and deployment regressions for the shared owner helper."""

import ast
import errno
from pathlib import Path
import subprocess
import sys
import threading
import types

import pytest

from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.virtuoso.basic.resources import ramic_owner


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM])
def test_initial_owner_read_denial_retains_ipc_fallback(monkeypatch, error):
    monkeypatch.setattr(ramic_owner, "_boot_id", lambda: "local-boot")

    def denied(pid):
        raise OSError(error, "process metadata unavailable")

    def forbidden(*args, **kwargs):
        pytest.fail("unverified owner caused a signal, exit or monitor")

    monkeypatch.setattr(ramic_owner, "_identity", denied)
    monkeypatch.setattr(ramic_owner.os, "kill", forbidden)
    monkeypatch.setattr(ramic_owner.threading, "Thread", forbidden)
    owner = ramic_owner.OwnerProcess(123, "local-boot")
    assert owner.alive() is None
    owner.interrupt()
    owner.start_monitor()
    # A later readable PID must not establish a replacement identity.
    monkeypatch.setattr(ramic_owner, "_identity", lambda pid: "replacement")
    assert owner.alive() is None
    owner.interrupt()


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM])
def test_transient_owner_read_denial_does_not_exit_monitor(monkeypatch, error):
    monkeypatch.setattr(ramic_owner, "_boot_id", lambda: "local-boot")
    monkeypatch.setattr(ramic_owner, "_identity", lambda pid: "original")
    owner = ramic_owner.OwnerProcess(123, "local-boot")
    targets = []

    class CapturedThread:
        def __init__(self, *, target, name):
            targets.append(target)

        def start(self):
            pass

    def forbidden(*args):
        pytest.fail("unavailable metadata is not confirmed owner death")

    monkeypatch.setattr(ramic_owner.threading, "Thread", CapturedThread)
    monkeypatch.setattr(ramic_owner.os, "_exit", forbidden)
    monkeypatch.setattr(ramic_owner.os, "kill", forbidden)
    owner.start_monitor()

    def denied(pid):
        raise OSError(error, "process metadata unavailable")

    monkeypatch.setattr(ramic_owner, "_identity", denied)
    assert owner.alive() is None
    owner.interrupt()
    pauses = iter([None])
    monkeypatch.setattr(ramic_owner.time, "sleep", lambda seconds: next(pauses))
    with pytest.raises(StopIteration):
        targets[0]()
    # Recovered access reconciles against the original, never a new baseline.
    monkeypatch.setattr(ramic_owner, "_identity", lambda pid: "original")
    assert owner.alive() is True
    monkeypatch.setattr(ramic_owner, "_identity", lambda pid: "replacement")
    assert owner.alive() is False


@pytest.mark.parametrize("version", ["3", "27"])
def test_actual_watchdog_without_verified_owner_never_signals(version):
    resource = Path(ramic_owner.__file__).parent / f"ramic_bridge_daemon_{version}.py"
    tree = ast.parse(resource.read_text(encoding="ascii"))
    callback = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "watchdog_callback")
    signals = []
    namespace = {"_owner": None, "virtuoso_pid": 123,
                 "os": types.SimpleNamespace(kill=lambda *args: signals.append(args)),
                 "signal": types.SimpleNamespace(SIGINT=2)}
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(resource), "exec"), namespace)
    expired = threading.Event()
    namespace["watchdog_callback"](expired, threading.Event(), threading.Lock())
    assert expired.is_set()
    assert signals == []


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

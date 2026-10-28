"""PID checks must not send Windows console events or leak process handles."""

from __future__ import annotations

import ctypes
import os
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from virtuoso_bridge.transport import ssh, tunnel


@pytest.mark.parametrize("saved,current", [(None, "birth2"), ("birth1", "birth2")])
def test_stop_rejects_unverified_or_reused_adopted_pid(monkeypatch, saved, current):
    runner = ssh.SSHRunner("fixture")
    runner.tunnel_pid = 42
    runner._saved_tunnel_identity = saved
    monkeypatch.setattr(ssh, "_process_identity", lambda pid: current)
    monkeypatch.setattr(ssh.os, "kill", lambda *a: pytest.fail("must not signal"))
    with pytest.raises(RuntimeError, match="unverified or reused"):
        runner.stop_port_forward()
    assert runner.tunnel_pid == 42
    runner.detach_port_forward()


def test_stop_verified_adopted_pid_and_clear_identity(monkeypatch):
    runner = ssh.SSHRunner("fixture")
    runner.tunnel_pid = 42
    runner._saved_tunnel_identity = "birth1"
    identities = iter(["birth1", None])
    monkeypatch.setattr(ssh, "_process_identity", lambda pid: next(identities))
    calls = []
    monkeypatch.setattr(ssh.os, "kill", lambda *a: calls.append(a))
    runner.stop_port_forward()
    assert calls == [(42, ssh.signal.SIGTERM)]
    assert runner.tunnel_pid is None
    assert runner._saved_tunnel_identity is None


def test_process_identity_rejects_current_non_ssh_process():
    assert ssh._process_identity(os.getpid()) is None


def test_stop_waits_for_adopted_pid_instead_of_an_old_exited_child(monkeypatch):
    runner = ssh.SSHRunner("fixture")
    runner._tunnel_proc = SimpleNamespace(pid=41, poll=lambda: 0,
                                         wait=lambda **kw: pytest.fail("waited for unrelated old child"))
    runner.tunnel_pid = 42
    runner._saved_tunnel_identity = "birth1"
    identities = iter(["birth1", None])
    monkeypatch.setattr(ssh, "_process_identity", lambda pid: next(identities))
    signals = []
    monkeypatch.setattr(ssh.os, "kill", lambda *args: signals.append(args))
    runner.stop_port_forward()
    assert signals == [(42, ssh.signal.SIGTERM)]


@pytest.mark.skipif(sys.platform != "linux", reason="native Linux SSH argv/UID/birth verification")
def test_legacy_adoption_and_stop_of_fixture_owned_native_ssh():
    executable = shutil.which("ssh")
    if not executable:
        pytest.skip("requires OpenSSH")
    # A private loopback stub holds the proxy handshake, so this real ssh
    # process never reaches credentials, a remote host or a user CIW.
    with socket.socket() as stub:
        stub.bind(("127.0.0.1", 0))
        stub.listen()
        args = [executable, "-F", "/dev/null", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                "-o", "ControlPersist=no", "-o", "ConnectTimeout=5", "-J",
                "127.0.0.1:%d" % stub.getsockname()[1], "-N", "-L", "12345:127.0.0.1:23456",
                "fixture@fixture.example.invalid"]
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        runner = ssh.SSHRunner("fixture.example.invalid", user="fixture")
        try:
            deadline = time.monotonic() + 3
            while not ssh._process_identity(proc.pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert proc.poll() is None
            runner.tunnel_pid = proc.pid
            runner.adopt_legacy_forward(12345, 23456)
            assert runner._saved_tunnel_identity == ssh._process_identity(proc.pid)
            runner.stop_port_forward()
            proc.wait(timeout=5)
            assert runner.tunnel_pid is None
        finally:
            runner.detach_port_forward()
            runner.close()
            try:
                os.killpg(proc.pid, ssh.signal.SIGTERM)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)


def _forbid_signals(*_args):
    raise AssertionError("Windows liveness checks must not call os.kill")


def _fake_os(monkeypatch, name, kill):
    # Replace module references, not os.name on the shared standard-library module.
    fake = SimpleNamespace(name=name, kill=kill)
    monkeypatch.setattr(ssh, "os", fake)
    monkeypatch.setattr(tunnel, "os", fake)


def _windows_api(monkeypatch, *, handle=123, wait_result=0x102, error=0):
    kernel = SimpleNamespace(
        OpenProcess=Mock(return_value=handle),
        WaitForSingleObject=Mock(return_value=wait_result),
        CloseHandle=Mock(return_value=True),
    )
    loader = Mock(return_value=kernel)
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    _fake_os(monkeypatch, "nt", _forbid_signals)
    return kernel, loader


@pytest.mark.parametrize("wait_result,expected", [(0x102, True), (0, False), (0xFFFFFFFF, False)])
def test_windows_wait_queries_without_signalling_and_closes_handle(monkeypatch, wait_result, expected):
    handle = 0x12345678ABCDEF12
    kernel, loader = _windows_api(monkeypatch, handle=handle, wait_result=wait_result)

    assert tunnel._pid_is_alive(42) is expected

    loader.assert_called_once_with("kernel32", use_last_error=True)
    kernel.OpenProcess.assert_called_once_with(0x00100000, False, 42)
    kernel.WaitForSingleObject.assert_called_once_with(handle, 0)
    kernel.CloseHandle.assert_called_once_with(handle)
    # HANDLE is pointer-sized; declaring ctypes signatures avoids 64-bit truncation.
    from ctypes import wintypes

    assert kernel.OpenProcess.restype is wintypes.HANDLE
    assert kernel.WaitForSingleObject.argtypes == [wintypes.HANDLE, wintypes.DWORD]
    assert kernel.CloseHandle.argtypes == [wintypes.HANDLE]


@pytest.mark.parametrize("error,expected", [(5, True), (87, False), (6, False)])
def test_windows_open_failure_never_waits_on_or_closes_invalid_handle(monkeypatch, error, expected):
    kernel, _loader = _windows_api(monkeypatch, handle=0, error=error)

    assert tunnel._pid_is_alive(42) is expected

    kernel.WaitForSingleObject.assert_not_called()
    kernel.CloseHandle.assert_not_called()


def test_windows_wait_error_still_closes_handle(monkeypatch):
    kernel, _loader = _windows_api(monkeypatch)
    kernel.WaitForSingleObject.side_effect = OSError("wait failed")

    assert not tunnel._pid_is_alive(42)

    kernel.CloseHandle.assert_called_once_with(123)


@pytest.mark.parametrize("pid", [2**32, 2**80])
def test_windows_rejects_pid_outside_dword_without_truncating(monkeypatch, pid):
    kernel, loader = _windows_api(monkeypatch)

    assert not tunnel._pid_is_alive(pid)

    loader.assert_not_called()
    kernel.OpenProcess.assert_not_called()


def test_windows_accepts_dword_boundary_without_wrapping(monkeypatch):
    kernel, _loader = _windows_api(monkeypatch, handle=0, error=87)

    assert not tunnel._pid_is_alive(2**32 - 1)

    kernel.OpenProcess.assert_called_once_with(0x00100000, False, 2**32 - 1)


@pytest.mark.parametrize("name", ["posix", "nt"])
@pytest.mark.parametrize("pid", [None, "invalid", 0, -1])
def test_invalid_pid_does_not_query_or_signal(monkeypatch, name, pid):
    _fake_os(monkeypatch, name, _forbid_signals)
    assert not tunnel._pid_is_alive(pid)


@pytest.mark.parametrize(
    "exception,expected",
    [(None, True), (ProcessLookupError(), False), (PermissionError(), True), (OSError(), False)],
)
def test_posix_preserves_signal_zero_probe(monkeypatch, exception, expected):
    kill = Mock(side_effect=exception)
    _fake_os(monkeypatch, "posix", kill)

    assert tunnel._pid_is_alive("42") is expected

    kill.assert_called_once_with(42, 0)


@pytest.fixture(params=["no_window", "detached"])
def windows_child(request):
    if os.name != "nt":
        pytest.skip("requires native Windows process handles")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    flags |= (
        subprocess.CREATE_NO_WINDOW
        if request.param == "no_window"
        else subprocess.DETACHED_PROCESS
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        yield proc
    finally:
        # Only the subprocess created by this fixture is terminated.
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.parametrize("path", ["saved_pid", "external_forward", "adopt_saved", "is_running"])
def test_windows_background_process_survives_all_liveness_paths(monkeypatch, windows_child, path):
    proc = windows_child
    monkeypatch.setattr(os, "kill", _forbid_signals)
    if path == "saved_pid":
        alive = tunnel._pid_is_alive(proc.pid)
    elif path == "external_forward":
        runner = SimpleNamespace(
            _tunnel_proc=None, _tunnel_using_external=True, _tunnel_pid=proc.pid,
        )
        alive = ssh.SSHRunner.is_tunnel_alive.fget(runner)
    else:
        state = {
            "mode": "remote", "port": 65433, "remote_port": 65432,
            "daemon_host": "eda.example", "tunnel_pid": proc.pid,
        }
        monkeypatch.setattr(tunnel.SSHClient, "read_state", staticmethod(lambda profile=None, **_: state))
        # No network is opened: this test concerns the process check only.
        monkeypatch.setattr(ssh.SSHRunner, "can_reach_port", staticmethod(lambda port: port == 65433))
        if path == "is_running":
            alive = tunnel.SSHClient.is_running()
        else:
            runner = SimpleNamespace(tunnel_pid=None)
            client = SimpleNamespace(
                _profile=None, _daemon_host="eda.example", _port=65432, target="eda.example:65432",
                read_state=lambda profile=None, **_: state, _require_runner=lambda: runner,
            )
            alive = tunnel.SSHClient._adopt_saved_tunnel(client, 65433)
            assert runner.tunnel_pid == proc.pid
    assert alive
    assert proc.poll() is None


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows process handles")
def test_windows_exited_process_is_not_alive(monkeypatch):
    with subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as proc:
        proc.wait(timeout=5)
        monkeypatch.setattr(os, "kill", _forbid_signals)
        assert not tunnel._pid_is_alive(proc.pid)


@pytest.mark.skipif(os.name != "posix", reason="requires native POSIX signal-zero semantics")
def test_posix_live_and_reaped_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert tunnel._pid_is_alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)
    assert not tunnel._pid_is_alive(proc.pid)

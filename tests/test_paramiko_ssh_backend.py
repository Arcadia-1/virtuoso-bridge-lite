from __future__ import annotations

import threading
import time
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import virtuoso_bridge.transport.tunnel as tunnel_module
from virtuoso_bridge.transport.paramiko_backend import (
    ParamikoSessionBackend,
    _Deadline,
    _Endpoint,
)
from virtuoso_bridge.transport.ssh import SSHRunner
from virtuoso_bridge.transport.tunnel import SSHClient


class _DispatchBackend:
    instance: "_DispatchBackend | None" = None

    def __init__(self, **kwargs) -> None:
        self.init_args = kwargs
        self.calls: list[tuple] = []
        _DispatchBackend.instance = self

    def test_connection(self, timeout) -> bool:
        self.calls.append(("test_connection", timeout))
        return True

    def run_command(self, command, *, timeout):
        self.calls.append(("run_command", command, timeout))
        return 0, "command output", ""

    def upload(self, local_path, remote_path, *, timeout):
        self.calls.append(("upload", local_path, remote_path, timeout))
        return 0, "", ""

    def upload_batch(self, files, *, timeout):
        self.calls.append(("upload_batch", files, timeout))
        return 0, "", ""

    def upload_text(self, contents, remote_path, *, timeout):
        self.calls.append(("upload_text", contents, remote_path, timeout))
        return 0, "", ""

    def download(self, remote_path, local_path, *, recursive, timeout):
        self.calls.append(
            ("download", remote_path, local_path, recursive, timeout)
        )
        return 0, "", ""

    def close(self) -> None:
        self.calls.append(("close",))


def test_ssh_runner_dispatches_to_explicit_paramiko_backend(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh._setup_command_log", lambda: None)
    monkeypatch.setattr(
        "virtuoso_bridge.transport.paramiko_backend.ParamikoSessionBackend",
        _DispatchBackend,
    )

    runner = SSHRunner(
        host="compute",
        user="designer",
        jump_host="bastion",
        jump_user="designer",
        backend="paramiko",
        max_sessions=7,
    )
    backend = _DispatchBackend.instance
    assert backend is not None
    assert backend.init_args["max_sessions"] == 7
    assert runner.backend == "paramiko"
    assert runner.max_sessions == 7
    assert not runner._use_control_master
    assert not runner.persistent_shell_enabled

    local_file = tmp_path / "input.scs"
    local_file.write_text("simulator lang=spectre\n", encoding="utf-8")
    assert runner.test_connection(timeout=2)
    assert runner.run_command("printf ok", timeout=3).stdout == "command output"
    assert runner.upload(local_file, "/tmp/input.scs", timeout=4).returncode == 0
    assert runner.upload_batch(
        [(local_file, "/tmp/batch.scs")], timeout=5
    ).returncode == 0
    assert runner.upload_text("payload", "/tmp/payload", timeout=6).returncode == 0
    assert runner.download(
        "/tmp/results", tmp_path / "results", recursive=True, timeout=7
    ).returncode == 0
    runner.close()

    assert [call[0] for call in backend.calls] == [
        "test_connection",
        "run_command",
        "upload",
        "upload_batch",
        "upload_text",
        "download",
        "close",
    ]


def test_profile_specific_paramiko_settings_reach_ssh_runner(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _CapturedRunner:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(tunnel_module, "load_vb_env", lambda: None)
    monkeypatch.setattr(tunnel_module, "resolve_profile", lambda profile: profile)
    monkeypatch.setattr(tunnel_module, "SSHRunner", _CapturedRunner)
    monkeypatch.setenv("VB_REMOTE_HOST_worker", "compute")
    monkeypatch.setenv("VB_REMOTE_USER_worker", "designer")
    monkeypatch.setenv("VB_JUMP_HOST_worker", "bastion")
    monkeypatch.setenv("VB_SSH_BACKEND_worker", "paramiko")
    monkeypatch.setenv("VB_SSH_MAX_SESSIONS_worker", "255")

    client = SSHClient.from_env(profile="worker")

    assert client.ssh_runner is not None
    assert captured["backend"] == "paramiko"
    assert captured["max_sessions"] == 255


class _FakeSSHException(Exception):
    pass


class _SessionTracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.opened = 0

    def open(self) -> None:
        with self.lock:
            self.active += 1
            self.opened += 1
            self.maximum = max(self.maximum, self.active)

    def close(self) -> None:
        with self.lock:
            self.active -= 1


class _FakeChannel:
    def __init__(self, tracker: _SessionTracker) -> None:
        self._tracker = tracker
        self._ready_at = time.monotonic() + 0.05
        self._closed = False
        tracker.open()

    def settimeout(self, _timeout) -> None:
        pass

    def exec_command(self, _command) -> None:
        pass

    def sendall(self, _payload) -> None:
        pass

    def shutdown_write(self) -> None:
        pass

    def recv_ready(self) -> bool:
        return False

    def recv_stderr_ready(self) -> bool:
        return False

    def exit_status_ready(self) -> bool:
        return time.monotonic() >= self._ready_at

    def recv_exit_status(self) -> int:
        return 0

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._tracker.close()


class _FakeTransport:
    def __init__(self, tracker: _SessionTracker) -> None:
        self._tracker = tracker

    def is_active(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    def open_session(self, timeout):
        assert timeout > 0
        return _FakeChannel(self._tracker)


class _FakeClient:
    def __init__(self, transport: _FakeTransport) -> None:
        self._transport = transport
        self.closed = False

    def get_transport(self) -> _FakeTransport:
        return self._transport

    def close(self) -> None:
        self.closed = True


def test_paramiko_backend_bounds_channels_on_one_transport() -> None:
    tracker = _SessionTracker()
    transport = _FakeTransport(tracker)
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(SSHException=_FakeSSHException)
    backend._session_gate = threading.BoundedSemaphore(2)
    backend._connect_lock = threading.RLock()
    backend._target_client = _FakeClient(transport)
    backend._jump_client = None
    backend._jump_channel = None
    backend.ensure_connected = lambda timeout=None: None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda i: backend.run_command(f"job {i}", 2), range(6)))

    assert all(returncode == 0 for returncode, _stdout, _stderr in results)
    assert tracker.opened == 6
    assert tracker.maximum == 2
    assert tracker.active == 0


class _RejectOnceTransport(_FakeTransport):
    def __init__(self, tracker: _SessionTracker) -> None:
        super().__init__(tracker)
        self._rejected = False

    def open_session(self, timeout):
        if not self._rejected:
            self._rejected = True
            raise _FakeSSHException("server refused one channel")
        return super().open_session(timeout)


def test_channel_rejection_does_not_close_shared_transport() -> None:
    tracker = _SessionTracker()
    transport = _RejectOnceTransport(tracker)
    client = _FakeClient(transport)
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(SSHException=_FakeSSHException)
    backend._session_gate = threading.BoundedSemaphore(255)
    backend._connect_lock = threading.RLock()
    backend._target_client = client
    backend._jump_client = None
    backend._jump_channel = None
    backend.ensure_connected = lambda timeout=None: None

    rejected = backend.run_command("first", 2)
    accepted = backend.run_command("second", 2)

    assert rejected == (255, "", "server refused one channel")
    assert accepted[0] == 0
    assert not client.closed


def test_sftp_file_error_does_not_close_active_transport(tmp_path: Path) -> None:
    tracker = _SessionTracker()
    transport = _FakeTransport(tracker)
    client = _FakeClient(transport)
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(SSHException=_FakeSSHException)
    backend._connect_lock = threading.RLock()
    backend._target_client = client
    backend._jump_client = None
    backend._jump_channel = None

    class _MissingRemoteFile:
        def get(self, _remote_path, _local_path, callback=None) -> None:
            raise FileNotFoundError(2, "remote file not found")

    @contextmanager
    def missing_sftp(_deadline, _command):
        yield _MissingRemoteFile()

    backend._sftp = missing_sftp
    local_path = tmp_path / "missing.fc"
    local_path.write_text("existing result\n", encoding="utf-8")

    result = backend.download(
        "/remote/missing.fc",
        local_path,
        recursive=False,
        timeout=2,
    )

    assert result[0] == 1
    assert "remote file not found" in result[2]
    assert not client.closed
    assert backend._target_client is client
    assert local_path.read_text(encoding="utf-8") == "existing result\n"
    assert not list(tmp_path.glob(".vbtmp-*"))


def test_nonrecursive_download_replaces_existing_file_after_success(
    tmp_path: Path,
) -> None:
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)

    class _SuccessfulSftp:
        def get(self, _remote_path, local_path, callback=None) -> None:
            Path(local_path).write_text("new result\n", encoding="utf-8")
            if callback is not None:
                callback(11, 11)

    @contextmanager
    def successful_sftp(_deadline, _command):
        yield _SuccessfulSftp()

    backend._sftp = successful_sftp
    local_path = tmp_path / "spectre.fc"
    local_path.write_text("old result\n", encoding="utf-8")

    result = backend.download(
        "/remote/spectre.fc",
        local_path,
        recursive=False,
        timeout=2,
    )

    assert result == (0, "", "")
    assert local_path.read_text(encoding="utf-8") == "new result\n"
    assert not list(tmp_path.glob(".vbtmp-*"))


def test_inactive_transport_is_closed_after_operation_error() -> None:
    tracker = _SessionTracker()
    transport = _FakeTransport(tracker)
    client = _FakeClient(transport)
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._connect_lock = threading.RLock()
    backend._target_client = client
    backend._jump_client = None
    backend._jump_channel = None
    transport.is_active = lambda: False

    backend._invalidate_if_transport_failed(OSError("connection lost"))

    assert client.closed
    assert backend._target_client is None


class _ChangedHostKey(Exception):
    pass


class _RejectChangedHostKeyClient:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.closed = False

    def load_system_host_keys(self, filename=None) -> None:
        self.events.append(
            "load-default" if filename is None else f"load:{filename}"
        )

    def set_missing_host_key_policy(self, _policy) -> None:
        self.events.append("set-policy")

    def connect(self, **_kwargs) -> None:
        self.events.append("connect")
        if "load-default" not in self.events:
            raise AssertionError("known_hosts must be loaded before connect")
        raise _ChangedHostKey("target host key changed")

    def close(self) -> None:
        self.closed = True
        self.events.append("close")


def test_connect_loads_known_hosts_and_propagates_changed_key() -> None:
    client = _RejectChangedHostKeyClient()
    backend = ParamikoSessionBackend.__new__(ParamikoSessionBackend)
    backend._paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        AutoAddPolicy=lambda: object(),
    )
    endpoint = _Endpoint(
        hostname="compute",
        username="designer",
        port=22,
        key_filenames=(),
    )

    with pytest.raises(_ChangedHostKey, match="target host key changed"):
        backend._connect_client(endpoint, _Deadline.start(2))

    assert client.events[0] == "load-default"
    assert client.events.index("set-policy") < client.events.index("connect")
    assert all(
        not event.startswith("load")
        for event in client.events[client.events.index("set-policy") + 1 :]
    )
    assert client.closed


def test_concurrent_mkdir_accepts_directory_created_by_other_session() -> None:
    class _RacingSftp:
        def __init__(self) -> None:
            self.barrier = threading.Barrier(2)
            self.local = threading.local()
            self.lock = threading.Lock()
            self.exists = False
            self.mkdir_calls = 0

        def stat(self, _path):
            if not getattr(self.local, "initial_probe_done", False):
                self.local.initial_probe_done = True
                self.barrier.wait(timeout=2)
                raise FileNotFoundError(2, "not found")
            with self.lock:
                if not self.exists:
                    raise FileNotFoundError(2, "not found")
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)

        def mkdir(self, _path) -> None:
            with self.lock:
                self.mkdir_calls += 1
                if self.exists:
                    raise FileExistsError(17, "already exists")
                self.exists = True

    sftp = _RacingSftp()
    deadline = _Deadline.start(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                ParamikoSessionBackend._mkdir_p,
                sftp,
                "/scratch",
                deadline,
            )
            for _ in range(2)
        ]
        for future in futures:
            future.result()

    assert sftp.exists
    assert sftp.mkdir_calls == 2

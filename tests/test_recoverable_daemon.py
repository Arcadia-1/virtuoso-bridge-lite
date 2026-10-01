from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest

from virtuoso_bridge.virtuoso.basic.resources.ramic_request_recovery import (
    DEFAULT_MAX_CACHE_BYTES,
    DEFAULT_MAX_FINISHED,
    DEFAULT_MAX_FRAME_BYTES,
    DEFAULT_TTL_SECONDS,
    ExecutionUncertain,
    RecoverableRequestManager,
    RequestNotStarted,
    read_response_frame,
)


TOKEN = "ab" * 32
STX = b"\x02"


def _resources() -> Path:
    return Path(__file__).parents[1] / "src/virtuoso_bridge/virtuoso/basic/resources"


def _frame(*parts: object) -> bytes:
    result = bytearray()
    for part in parts:
        if isinstance(part, str):
            data = part.encode("utf-8")
        else:
            data = bytes(part)
        result += str(len(data)).encode("ascii") + b":" + data
    return bytes(result)


def _mac(*parts: object) -> str:
    return hmac.new(TOKEN.encode(), _frame(*parts), hashlib.sha256).hexdigest()


def _request_id(now: float | None = None, suffix: str = "1" * 20) -> str:
    now = time.time() if now is None else now
    return f"{int(now * 1000):012x}{suffix}"


def _payload(module, op: str, request_id: str, *, skill: str = "", nonce: str | None = None,
             instance: str | None = None) -> dict:
    nonce = nonce or uuid.uuid4().hex
    instance = instance or module._RECOVERY.instance_id
    payload = {
        "proto": 1,
        "nonce": nonce,
        "op": op,
        "request_id": request_id,
        "daemon_instance": instance,
    }
    if op == "submit":
        payload["skill"] = skill
    payload["mac"] = _mac(
        "vb1-recoverable", "1", nonce, op, request_id, instance,
        skill if op == "submit" else "",
    )
    return payload


def _decode_reply(raw: bytes, nonce: str, *, recovery: bool = True) -> dict:
    assert raw[:1] == STX
    response_mac = raw[1:65].decode("ascii")
    body = raw[65:]
    assert hmac.compare_digest(
        response_mac, _mac("vb1-response", nonce, STX, body)
    )
    result = json.loads(body)
    if recovery:
        assert set(result) <= {
            "request_id", "daemon_instance", "state", "response", "diagnostic"
        }
    return result


def _exchange(module, payload: dict, *, disconnect: bool = False) -> bytes:
    client, server = socket.socketpair()
    thread = threading.Thread(
        target=module.handle_external_connection, args=(server, ("local", 0))
    )
    thread.start()
    client.sendall(json.dumps(payload).encode("utf-8"))
    client.shutdown(socket.SHUT_WR)
    if disconnect:
        client.close()
        thread.join(2)
        assert not thread.is_alive()
        return b""
    chunks = []
    while True:
        chunk = client.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    client.close()
    thread.join(2)
    assert not thread.is_alive()
    return b"".join(chunks)


def _import_daemon(monkeypatch, tmp_path):
    fcntl_stub = types.SimpleNamespace(
        fcntl=lambda *args, **kwargs: 0, F_GETFL=3, F_SETFL=4
    )
    monkeypatch.setitem(sys.modules, "fcntl", fcntl_stub)
    stdin_stub = types.SimpleNamespace(
        fileno=lambda: 0,
        buffer=types.SimpleNamespace(read=lambda n=1: b""),
    )
    monkeypatch.setattr(sys, "stdin", stdin_stub)
    monkeypatch.setenv("RB_TOKEN_PATH", str(tmp_path / "bridge_token"))
    monkeypatch.setattr(sys, "argv", ["daemon", "127.0.0.1", "1"])
    name = "vb_recovery_daemon_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(
        name, str(_resources() / "ramic_bridge_daemon_3.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BRIDGE_TOKEN = TOKEN
    module._NONCE_MARK.clear()
    return module


def _wait_receipt(manager, request_id: str, state: str = "completed") -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = manager.receipt(request_id)
        if result["state"] == state:
            return result
        time.sleep(0.005)
    raise AssertionError(manager.receipt(request_id))


def test_defaults_are_bounded_and_keep_receipts_for_one_hour() -> None:
    assert DEFAULT_TTL_SECONDS >= 3600
    assert DEFAULT_MAX_FINISHED >= 128
    assert DEFAULT_MAX_FRAME_BYTES == 8 * 1024 * 1024
    assert DEFAULT_MAX_CACHE_BYTES == 32 * 1024 * 1024


def test_duplicate_submit_executes_exactly_once_and_conflict_rejects() -> None:
    manager = RecoverableRequestManager()
    request_id = _request_id()
    calls = []

    def execute(skill):
        calls.append(skill)
        return "\x02ok"

    manager.submit(request_id, "1+1", execute)
    done = _wait_receipt(manager, request_id)
    duplicate = manager.submit(request_id, "1+1", execute)
    conflict = manager.submit(request_id, "2+2", execute)
    assert done["response"] == "\x02ok"
    assert duplicate == done
    assert conflict["state"] == "rejected"
    assert calls == ["1+1"]


def test_timestamp_window_prevents_reexecution_after_ttl() -> None:
    now = [1_900_000_000.0]
    manager = RecoverableRequestManager(ttl_seconds=3600, clock=lambda: now[0])
    request_id = _request_id(now[0])
    calls = []
    manager.submit(request_id, "x", lambda skill: calls.append(skill) or "\x02x")
    _wait_receipt(manager, request_id)
    now[0] += 3601
    assert manager.receipt(request_id)["state"] == "unknown"
    retry = manager.submit(request_id, "x", lambda skill: calls.append(skill) or "\x02x")
    assert retry["state"] == "rejected"
    assert calls == ["x"]


def test_submit_clock_skew_bounds_and_receipts_are_unrestricted() -> None:
    now = [1_900_000_000.0]
    manager = RecoverableRequestManager(clock=lambda: now[0])
    old_id = _request_id(now[0] - 301)
    future_id = _request_id(now[0] + 31)
    assert manager.submit(old_id, "x", lambda _: "\x02x")["state"] == "rejected"
    assert manager.submit(future_id, "x", lambda _: "\x02x")["state"] == "rejected"
    assert manager.receipt(old_id)["state"] == "unknown"


def test_busy_and_rejected_are_only_submit_ack_states() -> None:
    manager = RecoverableRequestManager()
    assert manager.try_begin_legacy()[0]
    request_id = _request_id()
    ack = manager.submit(request_id, "x", lambda _: "\x02x")
    assert ack["state"] == "busy"
    assert manager.receipt(request_id)["state"] == "unknown"
    manager.finish_legacy()
    assert manager.submit(request_id, "x", lambda _: "\x02x")["state"] == "busy"


def test_capacity_and_ttl_refuse_without_evicting_young_receipts() -> None:
    now = [1_900_000_000.0]
    manager = RecoverableRequestManager(
        max_finished=1, ttl_seconds=3600, max_frame_bytes=8, max_cache_bytes=16,
        clock=lambda: now[0],
    )
    first = _request_id(now[0], "1" * 20)
    second = _request_id(now[0], "2" * 20)
    manager.submit(first, "a", lambda _: "\x02a")
    _wait_receipt(manager, first)
    assert manager.submit(second, "b", lambda _: "\x02b")["state"] == "rejected"
    assert manager.receipt(first)["response"] == "\x02a"
    now[0] += 3601
    assert manager.receipt(first)["state"] == "unknown"


def test_running_request_reserves_one_finished_receipt_slot() -> None:
    manager = RecoverableRequestManager(max_finished=1)
    release = threading.Event()
    first = _request_id(suffix="1" * 20)
    second = _request_id(suffix="2" * 20)

    def execute(_skill):
        release.wait(1)
        return "\x02done"

    manager.submit(first, "x", execute)
    assert manager.submit(second, "y", lambda _: "\x02y")["state"] == "rejected"
    release.set()
    _wait_receipt(manager, first)
    assert len(manager._entries) == 1


@pytest.mark.parametrize(
    ("failure", "state", "poisoned"),
    [
        (RequestNotStarted("before transmission"), "rejected", False),
        (ExecutionUncertain("after transmission"), "unknown", True),
    ],
)
def test_failure_before_and_after_transmission(failure, state, poisoned) -> None:
    manager = RecoverableRequestManager()
    request_id = _request_id()

    def execute(_):
        raise failure

    manager.submit(request_id, "x", execute)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and manager._entries[request_id]["state"] == "running":
        time.sleep(0.005)
    assert manager._entries[request_id]["state"] == state
    result = manager.receipt(request_id)
    assert result["state"] == ("unknown" if state == "rejected" else state)
    assert "response" not in result
    assert bool(manager.poisoned) is poisoned


def test_eof_after_transmission_is_unknown_and_poisons_lane() -> None:
    manager = RecoverableRequestManager()
    request_id = _request_id()

    def execute(_):
        return read_response_frame(lambda: b"").decode("utf-8")

    manager.submit(request_id, "x", execute)
    result = _wait_receipt(manager, request_id, "unknown")
    assert "closed" in result["diagnostic"]
    assert manager.poisoned


@pytest.mark.parametrize("fail_phase", ["construct", "start"])
def test_worker_creation_failure_does_not_leave_a_phantom_running_request(fail_phase):
    class FailingThread:
        def __init__(self, **kwargs):
            if fail_phase == "construct":
                raise RuntimeError("thread constructor failed")

        def start(self):
            raise RuntimeError("thread start failed")

    manager = RecoverableRequestManager(thread_factory=FailingThread)
    request_id = _request_id()
    calls = []
    ack = manager.submit(request_id, "x", lambda code: calls.append(code) or "\x02x")
    assert ack["state"] == "rejected" and not calls
    assert manager.try_begin_legacy()[0]
    manager.finish_legacy()
    assert manager.receipt(request_id)["state"] == "unknown"


def test_slow_request_uses_one_total_read_deadline(monkeypatch, tmp_path):
    module = _import_daemon(monkeypatch, tmp_path)
    now = [0.0]
    monkeypatch.setattr(module, "time", types.SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    timeouts = []

    class SlowConnection:
        def settimeout(self, seconds):
            timeouts.append(seconds)

        def recv(self, size):
            now[0] += 3
            return b"x"

    with pytest.raises(socket.timeout, match="read budget"):
        module._recv_request(SlowConnection())
    assert timeouts == [5.0, 2.0]


@pytest.mark.parametrize("response", ["", "unframed"])
def test_unframed_execution_hook_response_is_unknown_and_poisoned(response):
    manager = RecoverableRequestManager()
    request_id = _request_id()
    manager.submit(request_id, "x", lambda _: response)
    result = _wait_receipt(manager, request_id, "unknown")
    assert "response" not in result and manager.poisoned


def test_unexpected_legacy_failure_poisons_lane_before_next_submission(monkeypatch, tmp_path):
    from virtuoso_bridge import daemon_auth

    module = _import_daemon(monkeypatch, tmp_path)

    def unexpected(*args):
        raise RuntimeError("timer/reader failed after possible transmission")

    monkeypatch.setattr(module, "_transmit_and_read", unexpected)
    nonce = uuid.uuid4().hex
    payload = {"proto": 1, "nonce": nonce, "skill": "save()", "timeout": 1}
    payload["mac"] = daemon_auth.request_mac(TOKEN, nonce=nonce, skill="save()", timeout=1)
    raw = _exchange(module, payload)
    verified = daemon_auth.verify_response_bytes(raw, TOKEN, nonce)
    assert verified.startswith(b"\x15")
    assert module._RECOVERY.poisoned
    calls = []
    refusal = module._RECOVERY.submit(_request_id(), "second()", lambda code: calls.append(code) or "\x02t")
    assert refusal["state"] == "rejected" and calls == []


@pytest.mark.parametrize("raw", [b"\x02ok", b"\x15error"])
def test_recoverable_completions_update_existing_monitor_counters(monkeypatch, tmp_path, raw):
    module = _import_daemon(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "_transmit_and_read", lambda code: raw)
    emitted = []
    monkeypatch.setattr(module, "_emit_stat", lambda: emitted.append(True))
    assert module._execute_recoverable("x") == raw.decode()
    assert module._RB_CALLS == 1
    assert module._RB_ERRORS == int(raw[:1] != b"\x02")
    assert emitted == [True]


@pytest.mark.parametrize(
    "execute",
    [
        lambda _: "\x02" + ("x" * 9),
        lambda _: (_ for _ in ()).throw(RuntimeError("unexpected")),
    ],
)
def test_noncompleted_worker_results_never_retain_a_response(execute) -> None:
    manager = RecoverableRequestManager(max_frame_bytes=8, max_cache_bytes=16)
    request_id = _request_id()
    manager.submit(request_id, "x", execute)
    result = _wait_receipt(manager, request_id, "unknown")
    assert "response" not in result
    assert manager._cached_bytes == 0


def test_recoverable_worker_never_uses_watchdog_or_sigint(monkeypatch, tmp_path) -> None:
    module = _import_daemon(monkeypatch, tmp_path)
    release = threading.Event()
    monkeypatch.setattr(module.os, "kill", lambda *args: pytest.fail("os.kill called"))
    def execute(_skill):
        release.wait(1)
        return "\x02late"

    monkeypatch.setattr(module, "_execute_recoverable", execute)
    request_id = _request_id()
    submit = _payload(module, "submit", request_id, skill="x")
    ack = _decode_reply(_exchange(module, submit), submit["nonce"])
    assert ack["state"] == "running"

    hello_nonce = uuid.uuid4().hex
    hello = {
        "proto": 1, "nonce": hello_nonce, "op": "hello",
        "mac": _mac("vb1-hello", "1", hello_nonce),
    }
    hello_reply = _decode_reply(_exchange(module, hello), hello_nonce, recovery=False)
    assert hello_reply["recoverable_requests"] == 1
    assert hello_reply["daemon_instance"] == module._RECOVERY.instance_id
    receipt = _payload(module, "receipt", request_id)
    assert _decode_reply(_exchange(module, receipt), receipt["nonce"])["state"] == "running"
    release.set()
    _wait_receipt(module._RECOVERY, request_id)


def test_late_response_survives_submit_client_disconnect(monkeypatch, tmp_path) -> None:
    module = _import_daemon(monkeypatch, tmp_path)
    release = threading.Event()
    def execute(_skill):
        release.wait(1)
        return "\x02late"

    monkeypatch.setattr(module, "_execute_recoverable", execute)
    request_id = _request_id()
    submit = _payload(module, "submit", request_id, skill="x")
    _exchange(module, submit, disconnect=True)
    release.set()
    _wait_receipt(module._RECOVERY, request_id)
    receipt = _payload(module, "receipt", request_id)
    result = _decode_reply(_exchange(module, receipt), receipt["nonce"])
    assert result == {
        "request_id": request_id,
        "daemon_instance": module._RECOVERY.instance_id,
        "state": "completed",
        "response": "\x02late",
    }


def test_old_and_new_calls_share_one_lane() -> None:
    manager = RecoverableRequestManager()
    release = threading.Event()
    first = _request_id(suffix="1" * 20)
    second = _request_id(suffix="2" * 20)
    def execute(_skill):
        release.wait(1)
        return "\x02x"

    manager.submit(first, "x", execute)
    assert manager.try_begin_legacy() == (False, "IPC lane is busy")
    assert manager.submit(second, "y", lambda _: "\x02y")["state"] == "busy"
    release.set()
    _wait_receipt(manager, first)


def test_recovery_auth_replay_tamper_and_instance_rejection(monkeypatch, tmp_path) -> None:
    module = _import_daemon(monkeypatch, tmp_path)
    request_id = _request_id()
    original = _payload(module, "submit", request_id, skill="1+1")
    for field, value in [
        ("proto", 2), ("nonce", "cd" * 16), ("op", "receipt"),
        ("request_id", _request_id(suffix="2" * 20)),
        ("daemon_instance", "0" * 32), ("skill", "2+2"),
    ]:
        tampered = dict(original)
        tampered[field] = value
        assert module._auth_error(tampered, "recoverable")
    assert module._auth_error(original, "recoverable") is None
    assert "replayed" in module._auth_error(original, "recoverable")

    wrong = _payload(module, "submit", request_id, skill="1+1", instance="0" * 32)
    result = _decode_reply(_exchange(module, wrong), wrong["nonce"])
    assert result["state"] == "rejected"
    assert "mismatch" in result["diagnostic"]


def test_legacy_watchdog_poison_blocks_recoverable_without_consuming_late_frame(
    monkeypatch, tmp_path
) -> None:
    module = _import_daemon(monkeypatch, tmp_path)
    chunks = iter([b"\x02", b"l", b"a", b"t", b"e", b"\x1e"])

    class _Timer:
        daemon = True

        def __init__(self, _seconds, callback, args=()):
            self.callback = callback
            self.args = args

        def start(self):
            self.callback(*self.args)

        def cancel(self):
            pass

    fake_stdout = types.SimpleNamespace(
        buffer=types.SimpleNamespace(write=lambda data: len(data), flush=lambda: None)
    )
    fake_stdin = types.SimpleNamespace(
        buffer=types.SimpleNamespace(read=lambda n=1: next(chunks))
    )
    monkeypatch.setattr(module, "threading", types.SimpleNamespace(
        Event=threading.Event, Timer=_Timer
    ))
    monkeypatch.setattr(module.sys, "stdout", fake_stdout)
    monkeypatch.setattr(module.sys, "stdin", fake_stdin)
    monkeypatch.setattr(module.os, "kill", lambda *args: None)
    acquired, _ = module._RECOVERY.try_begin_legacy()
    assert acquired
    with pytest.raises(ExecutionUncertain, match="watchdog fired") as caught:
        module._transmit_and_read("1+1", 0.01)
    module._RECOVERY.finish_legacy(str(caught.value))

    called = []
    rejected = module._RECOVERY.submit(
        _request_id(), "2+2", lambda skill: called.append(skill) or "\x02new"
    )
    assert rejected["state"] == "rejected"
    assert called == []
    assert list(chunks) == [b"\x02", b"l", b"a", b"t", b"e", b"\x1e"]


def test_python2_sources_are_ascii_and_optional_runtime_compiles() -> None:
    for name in ("ramic_request_recovery.py", "ramic_bridge_daemon_27.py"):
        path = _resources() / name
        path.read_bytes().decode("ascii")
    python2 = shutil.which("python2.7")
    if python2 is None:
        pytest.skip("python2.7 is not installed")
    result = subprocess.run(
        [python2, "-m", "py_compile", str(_resources() / "ramic_request_recovery.py"),
         str(_resources() / "ramic_bridge_daemon_27.py")],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

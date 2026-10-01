"""Real localhost sockets, actual daemon handlers, simulated CIW operations."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.dialogs import DialogInspection, DialogTarget
from tests.test_recoverable_daemon import TOKEN, _import_daemon


@pytest.fixture
def daemon_transport(monkeypatch, tmp_path):
    module = _import_daemon(monkeypatch, tmp_path)
    module.virtuoso_pid = 42
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.05)
    stop = threading.Event()
    handlers = []

    def serve():
        while not stop.is_set():
            try:
                conn, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=module.handle_external_connection, args=(conn, addr))
            thread.start()
            handlers.append(thread)

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        yield module, listener.getsockname()[1]
    finally:
        stop.set()
        listener.close()
        worker.join(2)
        for handler in handlers:
            handler.join(2)
            assert not handler.is_alive()
        assert not worker.is_alive()


def _client(monkeypatch, port, inspect):
    client = VirtuosoClient(host="127.0.0.1", port=port, daemon_token=TOKEN)
    monkeypatch.setattr(client.dialogs, "inspect", inspect)
    client.dialogs.enable_guard(local_gui=True, protect_inflight=True)
    return client


def test_popup_returns_handle_then_recovers_original_result_over_tcp(
    daemon_transport, monkeypatch,
):
    module, port = daemon_transport
    active = threading.Event()
    release = threading.Event()
    calls = []

    def execute(skill):
        calls.append(skill)
        active.set()
        assert release.wait(5)
        return "\x023"

    monkeypatch.setattr(module, "_execute_recoverable", execute)
    monkeypatch.setattr(module.os, "kill", lambda *a: pytest.fail("must not interrupt Virtuoso"))
    target = DialogTarget(pid=42, display=":7", ciw_window="0x10")

    def inspect(**kwargs):
        return DialogInspection(
            status="blocked" if active.is_set() else "clear", target=target,
            dialogs=[{"window_id": "0x20", "source": "unknown"}] if active.is_set() else [],
        )

    client = _client(monkeypatch, port, inspect)
    try:
        result = client.execute_skill("1+2", timeout=4)
        assert result.metadata["phase"] == "awaiting_user"
        assert result.metadata["waiting_for_user"] is True
        handle = result.metadata["request_handle"]
        assert client.requests.receipt(handle).metadata["request_state"] == "running"
        legacy = VirtuosoClient(host="127.0.0.1", port=port, daemon_token=TOKEN)
        refused = legacy.execute_skill("second()", timeout=1, retry_connect=False)
        assert not refused.ok and "busy" in refused.errors[0].lower()
        assert calls == ["1+2"]
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        original = client.requests.receipt(handle)
        if original.metadata["request_state"] == "completed":
            break
        time.sleep(0.01)
    assert original.ok and original.output == "3"
    assert calls == ["1+2"]


def test_lost_acknowledgement_recovers_without_another_submission(
    daemon_transport, monkeypatch,
):
    module, port = daemon_transport
    release = threading.Event()
    calls = []

    def execute(skill):
        calls.append(skill)
        assert release.wait(5)
        return "\x02saved"

    monkeypatch.setattr(module, "_execute_recoverable", execute)
    target = DialogTarget(pid=42, display=":7", ciw_window="0x10")
    client = _client(monkeypatch, port, lambda **k: DialogInspection(status="clear", target=target))
    exchange = client._exchange_payload

    def drop_ack(payload, deadline, **kwargs):
        reply = exchange(payload, deadline, **kwargs)
        if payload.get("op") == "submit":
            raise ConnectionResetError("simulated acknowledgement loss after server acceptance")
        return reply

    monkeypatch.setattr(client, "_exchange_payload", drop_ack)
    try:
        result = client.execute_skill("save()", timeout=2)
        assert result.metadata["phase"] == "submit"
        assert result.metadata["outcome"] == "unknown"
        handle = result.metadata["request_handle"]
    finally:
        release.set()
    monkeypatch.setattr(client, "_exchange_payload", exchange)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        original = client.requests.receipt(handle)
        if original.metadata["request_state"] == "completed":
            break
        time.sleep(0.01)
    assert original.ok and original.output == "saved"
    assert calls == ["save()"]

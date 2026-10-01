"""Offline regression tests for independent daemon response-write budgets."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import uuid
from pathlib import Path

import pytest


_RESOURCES = (
    Path(__file__).parents[1]
    / "src"
    / "virtuoso_bridge"
    / "virtuoso"
    / "basic"
    / "resources"
)


def _import_daemon(monkeypatch, tmp_path, filename):
    fcntl_stub = types.SimpleNamespace(
        fcntl=lambda *args, **kwargs: 0, F_GETFL=3, F_SETFL=4
    )
    monkeypatch.setitem(sys.modules, "fcntl", fcntl_stub)
    monkeypatch.setattr(
        sys,
        "stdin",
        types.SimpleNamespace(
            fileno=lambda: 0,
            buffer=types.SimpleNamespace(read=lambda size=1: b""),
        ),
    )
    token_path = tmp_path / "bridge_token"
    token_path.write_text("ab" * 32 + "\n", encoding="ascii")
    monkeypatch.setenv("RB_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(sys, "argv", ["daemon", "127.0.0.1", "1"])

    name = "vb_reply_budget_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, str(_RESOURCES / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if filename == "ramic_bridge_daemon_27.py":
        module.unicode = type("_Python2Unicode", (str,), {})
        module.basestring = (str, bytes)
    return module


class _SlowAuthErrorConnection:
    def __init__(self, payload, clock):
        self._payload = payload
        self._clock = clock
        self._reads = 0
        self.timeouts = []
        self.sent = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, _size):
        self._reads += 1
        if self._reads == 1:
            self._clock[0] += 4.9
            return self._payload
        return self._payload[:0]

    def sendall(self, data):
        self.sent.append((self.timeouts[-1], data))

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("filename", "wire_encoding"),
    [
        ("ramic_bridge_daemon_3.py", "bytes"),
        ("ramic_bridge_daemon_27.py", "text"),
    ],
)
def test_auth_error_reply_replaces_remaining_request_read_timeout(
    monkeypatch, tmp_path, filename, wire_encoding
):
    module = _import_daemon(monkeypatch, tmp_path, filename)
    clock = [0.0]
    module.time = types.SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0]
    )
    payload = json.dumps({"proto": 1, "skill": "1+1", "timeout": 1})
    if wire_encoding == "bytes":
        payload = payload.encode("utf-8")
    connection = _SlowAuthErrorConnection(payload, clock)

    module.handle_external_connection(connection, ("local", 0))

    assert module._CONNECTION_TIMEOUT == 5.0
    assert connection.timeouts[:2] == pytest.approx([5.0, 0.1])
    assert connection.sent
    write_timeout, response = connection.sent[0]
    assert write_timeout == module._RESPONSE_WRITE_TIMEOUT == 5.0
    assert response.startswith(b"\x15AuthError" if wire_encoding == "bytes" else "\x15AuthError")
    assert connection.closed

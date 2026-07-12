from __future__ import annotations

import errno
import socket
from collections.abc import Iterator
from typing import get_type_hints

import pytest

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.virtuoso.basic import bridge


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        assert seconds > 0.0
        self.sleeps.append(seconds)
        self.advance(seconds)


class _FakeSocket:
    def __init__(
        self,
        clock: _FakeClock,
        *,
        connect_duration: float = 0.0,
        connect_error: OSError | None = None,
        send_duration: float = 0.0,
        recv_results: tuple[tuple[float, bytes], ...] = ((0.0, b""),),
    ) -> None:
        self._clock = clock
        self._connect_duration = connect_duration
        self._connect_error = connect_error
        self._send_duration = send_duration
        self._recv_results: Iterator[tuple[float, bytes]] = iter(recv_results)
        self._timeout: float | None = None
        self.phase_timeouts: list[tuple[str, float]] = []

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def settimeout(self, timeout: float) -> None:
        self._timeout = timeout

    def connect(self, _address: tuple[str, int]) -> None:
        self._run_phase("connect", self._connect_duration)
        if self._connect_error is not None:
            raise self._connect_error

    def sendall(self, _payload: bytes) -> None:
        self._run_phase("send", self._send_duration)

    def shutdown(self, _how: int) -> None:
        return None

    def recv(self, _size: int) -> bytes:
        duration, result = next(self._recv_results)
        self._run_phase("recv", duration)
        return result

    def _run_phase(self, phase: str, duration: float) -> None:
        assert self._timeout is not None
        self.phase_timeouts.append((phase, self._timeout))
        if duration > self._timeout:
            self._clock.advance(self._timeout)
            raise socket.timeout
        self._clock.advance(duration)


class _SocketFactory:
    def __init__(self, sockets: list[_FakeSocket]) -> None:
        self._remaining = iter(sockets)
        self.created: list[_FakeSocket] = []

    def __call__(self, *_args: object) -> _FakeSocket:
        fake_socket = next(self._remaining)
        self.created.append(fake_socket)
        return fake_socket


def _use_fake_network(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    factory: _SocketFactory,
) -> None:
    monkeypatch.setattr(bridge.time, "monotonic", clock)
    monkeypatch.setattr(bridge.time, "sleep", clock.sleep)
    monkeypatch.setattr(bridge.socket, "socket", factory)
    monkeypatch.setenv("VB_JUMP_HOST", "jump.example.com")


def test_execute_skill_uses_one_deadline_across_retry_and_socket_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    refused = _FakeSocket(
        clock,
        connect_duration=0.1,
        connect_error=ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
    )
    connected = _FakeSocket(
        clock,
        connect_duration=0.1,
        send_duration=0.2,
        recv_results=((0.2, b"\x02partial"), (0.3, b"")),
    )
    factory = _SocketFactory([refused, connected])
    _use_fake_network(monkeypatch, clock, factory)

    result = VirtuosoClient().execute_skill("1+1", timeout=1.0)

    assert result.status == ExecutionStatus.ERROR
    assert result.errors == ["Socket timeout after 1.0s"]
    assert result.execution_time == pytest.approx(1.0)
    assert clock.sleeps == [0.2]
    assert refused.phase_timeouts == [("connect", pytest.approx(1.0))]
    assert connected.phase_timeouts == [
        ("connect", pytest.approx(0.7)),
        ("send", pytest.approx(0.6)),
        ("recv", pytest.approx(0.4)),
        ("recv", pytest.approx(0.2)),
    ]


def test_execute_skill_timeout_caps_jump_host_retry_grace(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _FakeClock()
    refused_sockets = [
        _FakeSocket(
            clock,
            connect_error=ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
        )
        for _ in range(20)
    ]
    factory = _SocketFactory(refused_sockets)
    _use_fake_network(monkeypatch, clock, factory)

    result = VirtuosoClient().execute_skill("1+1", timeout=0.1)

    assert result.status == ExecutionStatus.ERROR
    assert result.errors == ["Socket timeout after 0.1s"]
    assert result.execution_time == pytest.approx(0.1)
    assert clock.sleeps == [pytest.approx(0.1)]
    assert factory.created[0].phase_timeouts == [("connect", pytest.approx(0.1))]
    assert sum(
        phase == "connect"
        for fake_socket in factory.created
        for phase, _timeout in fake_socket.phase_timeouts
    ) == 1
    assert any("after 0.1s" in message for message in caplog.messages)


def test_execute_skill_timeout_annotation_accepts_float() -> None:
    annotations = get_type_hints(VirtuosoClient.execute_skill)

    assert annotations["timeout"] == float | None


def test_execute_skill_preserves_successful_jump_host_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    refused = _FakeSocket(
        clock,
        connect_duration=0.05,
        connect_error=ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
    )
    connected = _FakeSocket(
        clock,
        connect_duration=0.05,
        send_duration=0.05,
        recv_results=((0.05, b"\x023"), (0.0, b"")),
    )
    factory = _SocketFactory([refused, connected])
    _use_fake_network(monkeypatch, clock, factory)

    result = VirtuosoClient().execute_skill("1+2", timeout=1.0)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.output == "3"
    assert result.execution_time == pytest.approx(0.4)
    assert clock.sleeps == [0.2]
    assert len(factory.created) == 2

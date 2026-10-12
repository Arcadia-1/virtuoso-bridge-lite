"""A busy IPC lane is waited for instead of failing a parallel request.

The daemon answers ``BusyError`` before it transmits anything, so resending
cannot run a request twice.
"""

from __future__ import annotations

import pytest

from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.virtuoso.basic import bridge
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


def _client(monkeypatch, replies):
    client = VirtuosoClient(host="127.0.0.1", port=9, timeout=5)
    sent = []

    def once(skill, timeout, deadline):
        sent.append(skill)
        return replies.pop(0)

    sleeps = []
    monkeypatch.setattr(client, "_execute_skill_once", once)
    monkeypatch.setattr(bridge.time, "sleep", sleeps.append)
    return client, sent, sleeps


def test_busy_lane_is_retried_until_the_request_runs(monkeypatch):
    client, sent, sleeps = _client(monkeypatch, [
        "\x15BusyError: IPC lane is busy",
        "\x15BusyError: daemon connection limit reached",
        "\x02 42",
    ])

    result = client.execute_skill("1+41")

    assert result.status == ExecutionStatus.SUCCESS and result.output.strip() == "42"
    assert result.metadata["busy_retries"] == 2
    assert sleeps == [0.1, 0.2]
    assert sent == ["1+41"] * 3


def test_retrying_a_busy_lane_is_allowed_without_connect_retries(monkeypatch):
    client, sent, _ = _client(monkeypatch, ["\x15BusyError: IPC lane is busy", "\x02t"])

    assert client._execute_skill_unguarded("save()", 5, retry_connect=False).ok
    assert len(sent) == 2


@pytest.mark.parametrize("reply", [
    "\x15BusyError: IPC lane is poisoned: SKILL did not answer within the request timeout; "
    "waiting for Virtuoso to answer a resync probe",
    "\x15undefined function",
])
def test_stuck_lane_and_skill_errors_are_not_retried(monkeypatch, reply):
    client, sent, sleeps = _client(monkeypatch, [reply])

    result = client.execute_skill("x()")

    assert result.status == ExecutionStatus.ERROR and len(sent) == 1 and sleeps == []


def test_busy_retries_stop_at_the_request_timeout(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    client, sent, _ = _client(monkeypatch, ["\x15BusyError: IPC lane is busy"] * 50)
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    result = client.execute_skill("x()", timeout=3)

    assert result.errors == ["BusyError: IPC lane is busy"]
    assert clock[0] < 3
    assert 3 < len(sent) < 10

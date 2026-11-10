from pathlib import Path

import pytest

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult


def _client(monkeypatch, result):
    client = VirtuosoClient(daemon_token="fixture-token", timeout=7)
    calls = []

    def execute(skill, **kwargs):
        calls.append((skill, kwargs))
        return result

    monkeypatch.setattr(client, "execute_skill", execute)
    return client, calls


@pytest.mark.parametrize("options", [
    {}, {"display_resources": "refuse"}, {"display_resources": "save"},
    {"display_resources": "save", "display_path": ""},
    {"display_resources": "save", "display_path": "bad\npath"},
    {"display_resources": "discard", "display_path": "/tmp/unused.drf"},
    {"display_resources": "discard", "unsaved": "ignore"},
])
def test_invalid_exit_choice_sends_nothing(monkeypatch, options):
    client, calls = _client(monkeypatch, None)
    with pytest.raises((TypeError, ValueError)):
        client.request_exit(**options)
    assert calls == []


@pytest.mark.parametrize("status, metadata, state", [
    (ExecutionStatus.SUCCESS, {}, "requested"),
    (ExecutionStatus.ERROR, {"request_sent": False, "outcome": "not_started"}, "not_started"),
    (ExecutionStatus.ERROR, {"outcome": "unknown", "request_handle": {"id": "original"}}, "unknown"),
])
def test_exit_preserves_recovery_evidence_and_never_retries(monkeypatch, status, metadata, state):
    result = VirtuosoResult(status=status, output='"exit_requested"', metadata=dict(metadata))
    client, calls = _client(monkeypatch, result)
    actual = client.request_exit(display_resources="save", display_path=Path('/tmp/a"b.drf'), timeout=3)
    assert actual is result
    assert all(actual.metadata[k] == v for k, v in metadata.items())
    assert actual.metadata["exit_state"] == state
    assert len(calls) == 1
    assert calls[0][1] == {"timeout": 3, "retry_connect": False}


@pytest.mark.parametrize("policy", ["refuse", "save", "discard"])
def test_close_preserves_uncertain_result_and_never_retries(monkeypatch, policy):
    result = VirtuosoResult(status=ExecutionStatus.ERROR, metadata={"outcome": "unknown"})
    client, calls = _client(monkeypatch, result)
    assert client.close_windows("fixture", "target", unsaved=policy) is result
    assert result.metadata == {"outcome": "unknown"}
    assert len(calls) == 1
    assert calls[0][1] == {"timeout": 7, "retry_connect": False}

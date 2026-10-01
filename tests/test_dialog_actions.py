"""Client authorization tests; all X11 and identity operations are simulated."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.dialogs import DialogInspection, DialogTarget


HASH = "a" * 64
TARGET = DialogTarget(pid=42, display=":7", ciw_window="0x10")


def setup(monkeypatch):
    client = VirtuosoClient(daemon_token="ab" * 32)
    client.dialogs._target = TARGET
    client.dialogs._endpoint = (client.host, client.port)
    caps = {"auth": "on", "virtuoso_pid": 42, "daemon_instance": "d" * 32}
    monkeypatch.setattr(client, "_ensure_daemon_capabilities", lambda *a, **k: caps)
    monkeypatch.setattr(client, "execute_skill", lambda *a, **k: pytest.fail("No SKILL is allowed"))
    calls = []
    prepared = {"status": "prepared", "preview_png_b64": "cG5n", "snapshot": {
        "target": TARGET.model_dump(), "dialog": {"title": "Reviewed information", "window_id": "0x20"},
        "content_sha256": HASH, "process_start": "100",
    }}
    # Initialize the action facade without issuing a request.
    from virtuoso_bridge.virtuoso.dialog_actions import DialogActionOps
    actions = DialogActionOps(client.dialogs)
    client.dialogs._actions = actions

    def exchange(payload, deadline):
        calls.append(payload)
        return deepcopy(prepared) if payload["op"] == "prepare" else {"status": "requested", "action_sent": True}

    monkeypatch.setattr(actions, "_exchange", exchange)
    monkeypatch.setattr(client.dialogs, "inspect", lambda **k: DialogInspection(status="clear", target=TARGET))
    return client, actions, calls, caps, prepared


def ticket(client):
    return client.dialogs.prepare_close("0x20", expected_title="Reviewed information")


def test_default_never_authorizes_action(monkeypatch):
    client, _, calls, _, _ = setup(monkeypatch)
    preview = ticket(client)
    result = client.dialogs.close(preview, expected_content_sha256=HASH)
    assert result.status == "not_started" and result.action_sent is False
    assert [c["op"] for c in calls] == ["prepare"]


def test_approved_unchanged_snapshot_closes_once(monkeypatch):
    client, _, calls, _, _ = setup(monkeypatch)
    preview = ticket(client)
    result = client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH)
    assert result.status == "closed" and result.action_sent is True
    assert [c["op"] for c in calls] == ["prepare", "close"]
    again = client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH)
    assert again.status == "not_started" and len(calls) == 2


def test_unapproved_content_consumes_ticket_without_execution(monkeypatch):
    client, _, calls, _, _ = setup(monkeypatch)
    preview = ticket(client)
    result = client.dialogs.close(preview, authorized=True, expected_content_sha256="b" * 64)
    assert result.status == "not_started" and len(calls) == 1
    assert client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH).status == "not_started"


@pytest.mark.parametrize("field,value", [("virtuoso_pid", 99), ("auth", "off"), ("daemon_instance", "e" * 32)])
def test_identity_change_refuses_before_action(monkeypatch, field, value):
    client, _, calls, caps, _ = setup(monkeypatch)
    preview = ticket(client)
    caps[field] = value
    assert client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH).status == "not_started"
    assert len(calls) == 1


def test_endpoint_change_refuses(monkeypatch):
    client, _, calls, _, _ = setup(monkeypatch)
    preview = ticket(client)
    client.dialogs._endpoint = ("other", 1)
    assert client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH).status == "not_started"
    assert len(calls) == 1


def test_unknown_action_never_replays(monkeypatch):
    client, actions, calls, _, _ = setup(monkeypatch)
    preview = ticket(client)

    def lost(payload, deadline):
        calls.append(payload)
        raise TimeoutError("ack lost after possible close")

    monkeypatch.setattr(actions, "_exchange", lost)
    result = client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH)
    assert result.status == "unknown" and result.action_sent is None
    assert client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH).status == "not_started"
    assert len(calls) == 2


def test_backend_refusal_is_not_started(monkeypatch):
    client, actions, _, _, _ = setup(monkeypatch)
    preview = ticket(client)
    monkeypatch.setattr(actions, "_exchange", lambda *a: {"status": "not_started", "action_sent": False,
                                                        "diagnostic": "pixels changed"})
    result = client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH)
    assert result.status == "not_started" and result.action_sent is False


def test_requested_is_not_claimed_closed_when_inspection_uncertain(monkeypatch):
    client, _, _, _, _ = setup(monkeypatch)
    preview = ticket(client)
    monkeypatch.setattr(client.dialogs, "inspect", lambda **k: DialogInspection(status="indeterminate", target=TARGET))
    assert client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH).status == "requested"


def test_post_send_inspection_failure_preserves_delivery_without_retry(monkeypatch):
    client, _, calls, _, _ = setup(monkeypatch)
    preview = ticket(client)
    def failed(**kwargs):
        raise TimeoutError("inspection unavailable")
    monkeypatch.setattr(client.dialogs, "inspect", failed)
    result = client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH)
    assert result.status == "requested" and result.action_sent is True
    assert client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH).status == "not_started"
    assert len(calls) == 2


def test_authenticated_legacy_daemon_uses_native_process_binding(monkeypatch):
    client, _, calls, caps, _ = setup(monkeypatch)
    del caps["daemon_instance"]
    preview = ticket(client)
    result = client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH)
    assert result.status == "closed"
    assert [call["op"] for call in calls] == ["prepare", "close"]


def test_ticket_tampering_refuses(monkeypatch):
    client, _, calls, _, _ = setup(monkeypatch)
    altered = ticket(client).model_copy(update={"window_id": "0x99"})
    assert client.dialogs.close(altered, authorized=True, expected_content_sha256=HASH).status == "not_started"
    assert len(calls) == 1


def test_expiry_refuses(monkeypatch):
    client, actions, calls, _, _ = setup(monkeypatch)
    preview = ticket(client)
    entry = actions._tickets[preview.ticket_id]
    actions._tickets[preview.ticket_id] = (entry[0], 0, *entry[2:])
    assert client.dialogs.close(preview, authorized=True, expected_content_sha256=HASH).status == "not_started"
    assert len(calls) == 1


def test_guard_required_for_preparation(monkeypatch):
    client, _, calls, _, _ = setup(monkeypatch)
    client.dialogs.disable_guard()
    with pytest.raises(ValueError, match="guard"):
        ticket(client)
    assert not calls


@pytest.mark.parametrize("value", ["", "line\nbreak", " ", "x" * 513])
def test_invalid_exact_title_rejected(monkeypatch, value):
    client, _, calls, _, _ = setup(monkeypatch)
    with pytest.raises(ValueError):
        client.dialogs.prepare_close("0x20", expected_title=value)
    assert not calls


def test_wrong_prepared_target_refused(monkeypatch):
    client, _, calls, _, prepared = setup(monkeypatch)
    prepared["snapshot"]["target"]["pid"] = 99
    with pytest.raises(ValueError, match="target"):
        ticket(client)
    assert len(calls) == 1


def test_ticket_cannot_be_reused_by_another_client(monkeypatch):
    first, _, _, _, _ = setup(monkeypatch)
    preview = ticket(first)
    second, _, calls, _, _ = setup(monkeypatch)
    assert second.dialogs.close(preview, authorized=True, expected_content_sha256=HASH).status == "not_started"
    assert not calls

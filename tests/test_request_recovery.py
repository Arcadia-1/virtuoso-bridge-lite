"""Offline wire tests: simulated popups, no Cadence/X11/SSH side effects."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from virtuoso_bridge import VirtuosoClient, daemon_auth
from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.virtuoso.dialogs import DialogInspection, DialogTarget
from virtuoso_bridge.virtuoso.requests import RequestHandle
from virtuoso_bridge.virtuoso.requests import RequestRecoveryError
from virtuoso_bridge.virtuoso.maestro import writer
from virtuoso_bridge.virtuoso import requests as requests_module
from virtuoso_bridge.virtuoso.basic import bridge as bridge_module


TOKEN = "ab" * 32
INSTANCE = "cd" * 16


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def time(self):
        return 1790755200 + self.now


def setup_client(monkeypatch, states=None, status="clear"):
    client = VirtuosoClient(daemon_token=TOKEN)
    client._daemon_caps = {"proto": 1, "auth": "on", "virtuoso_pid": 42,
                           "recoverable_requests": 1, "daemon_instance": INSTANCE}
    client._tunnel = SimpleNamespace(gui_host="gui", daemon_host="gui", gui_runner=object())
    monkeypatch.setattr(client, "_ensure_daemon_capabilities", lambda *a, **k: client._daemon_caps)
    target = DialogTarget(pid=42, display=":7", ciw_window="0x10")
    reports = []

    def inspect(**kwargs):
        reports.append(kwargs)
        return DialogInspection(status=status, target=target,
                                dialogs=[{"window_id": "0x20"}] if status == "blocked" else [])

    monkeypatch.setattr(client.dialogs, "inspect", inspect)
    # Activation sees a clear report; subsequent inspection uses requested status.
    client.dialogs._target = target
    client.dialogs._endpoint = (client.host, client.port)
    client.dialogs._protect_inflight = True
    monkeypatch.setattr(client.dialogs, "preflight", lambda **k: None)
    monkeypatch.setattr(client, "_execute_skill_unguarded", lambda *a, **k: pytest.fail("no legacy fallback"))
    wire = []
    receipt_states = iter(states or ["running", "completed"])

    def exchange(payload, deadline, **kwargs):
        wire.append(payload)
        expected = daemon_auth.recoverable_mac(
            TOKEN, nonce=payload["nonce"], op=payload["op"], request_id=payload["request_id"],
            daemon_instance=payload["daemon_instance"], skill=payload.get("skill", ""),
        )
        assert payload["mac"] == expected
        assert "timeout" not in payload  # No server execution/interrupt timer.
        state = next(receipt_states, "running")
        data = {"request_id": payload["request_id"], "daemon_instance": INSTANCE, "state": state}
        if state == "completed":
            data["response"] = "\x023"
        body = json.dumps(data)
        mac = daemon_auth.response_mac(TOKEN, nonce=payload["nonce"], marker="\x02", body=body)
        return ("\x02" + mac + body).encode()

    monkeypatch.setattr(client, "_exchange_payload", exchange)
    clock = Clock()
    monkeypatch.setattr(requests_module, "time", clock)
    monkeypatch.setattr(bridge_module, "time", clock)
    return client, wire, reports, clock


def test_completed_execution_submits_exactly_once(monkeypatch):
    client, wire, reports, _ = setup_client(monkeypatch)
    result = client.execute_skill("1+2", timeout=5)
    assert result.ok and result.output == "3"
    assert result.metadata["outcome"] == "completed"
    assert [p["op"] for p in wire] == ["submit", "receipt"]
    assert wire[0]["request_id"] == wire[1]["request_id"]
    assert wire[0]["nonce"] != wire[1]["nonce"]
    assert reports == []


@pytest.mark.parametrize("status", ["blocked", "indeterminate"])
def test_request_triggered_popup_returns_without_cancelling_or_replaying(monkeypatch, status):
    client, wire, reports, _ = setup_client(monkeypatch, ["running"], status)
    result = client.execute_skill("save()", timeout=20)
    assert not result.ok
    assert result.metadata["phase"] == "awaiting_user"
    assert result.metadata["waiting_for_user"] == (status == "blocked")
    assert result.metadata["outcome"] == "unknown"
    assert result.metadata["request_sent"] is True
    assert [p["op"] for p in wire].count("submit") == 1
    assert all(p["op"] in ("submit", "receipt") for p in wire)
    assert len(reports) == 1 and reports[0]["pid"] == 42
    assert reports[0]["timeout"] == 5
    handle = RequestHandle.model_validate(result.metadata["request_handle"])
    assert handle.virtuoso_pid == 42


def test_wait_budget_retains_handle_and_never_resends(monkeypatch):
    client, wire, _, _ = setup_client(monkeypatch, ["running"])
    result = client.execute_skill("save()", timeout=2)
    assert result.metadata["phase"] == "wait_timeout"
    assert result.metadata["request_state"] == "running"
    assert result.metadata["request_handle"]
    assert sum(p["op"] == "submit" for p in wire) == 1


def test_lost_submit_acknowledgement_preserves_unknown_handle(monkeypatch):
    client, wire, _, _ = setup_client(monkeypatch)

    def dropped(payload, deadline, **kwargs):
        wire.append(payload)
        raise ConnectionResetError("after submission")

    monkeypatch.setattr(client, "_exchange_payload", dropped)
    result = client.execute_skill("save()", timeout=3)
    assert result.metadata["outcome"] == "unknown"
    assert result.metadata["request_sent"] is None
    assert result.metadata["request_handle"]
    assert len(wire) == 1


def test_lost_poll_returns_handle_without_replay(monkeypatch):
    client, wire, _, _ = setup_client(monkeypatch, ["running"])
    original = client._exchange_payload

    def dropped(payload, deadline, **kwargs):
        if payload["op"] == "receipt":
            raise ConnectionResetError("receipt transport lost")
        return original(payload, deadline)

    monkeypatch.setattr(client, "_exchange_payload", dropped)
    result = client.execute_skill("save()", timeout=3)
    assert result.metadata["phase"] == "receipt"
    assert result.metadata["request_sent"] is True
    assert len(wire) == 1


@pytest.mark.parametrize("state", ["busy", "rejected"])
def test_acknowledged_submission_refusal_is_not_started(monkeypatch, state):
    client, wire, _, _ = setup_client(monkeypatch, [state])
    result = client.execute_skill("save()", timeout=5)
    assert result.metadata["outcome"] == "not_started"
    assert result.metadata["request_sent"] is False
    assert len(wire) == 1


def test_receipt_query_works_while_guard_blocks_skill(monkeypatch):
    client, wire, _, _ = setup_client(monkeypatch, ["completed"])
    monkeypatch.setattr(client.dialogs, "preflight", lambda **k: pytest.fail("receipt is not SKILL"))
    monkeypatch.setattr(client.dialogs, "inspect", lambda **k: pytest.fail("no X11 needed"))
    handle = RequestHandle(request_id="ef" * 16, daemon_instance=INSTANCE, virtuoso_pid=42)
    result = client.requests.receipt(handle.model_dump())
    assert result.ok and result.output == "3"
    assert len(wire) == 1 and wire[0]["op"] == "receipt" and "skill" not in wire[0]


@pytest.mark.parametrize("state", ["unknown", "rejected", "busy", "running"])
def test_receipt_noncompletion_never_proves_original_did_not_execute(monkeypatch, state):
    client, wire, _, _ = setup_client(monkeypatch, [state])
    handle = RequestHandle(request_id="ef" * 16, daemon_instance=INSTANCE, virtuoso_pid=42)
    result = client.requests.receipt(handle)
    assert result.metadata["outcome"] == "unknown"
    assert not result.ok and wire[0]["op"] == "receipt"


@pytest.mark.parametrize("caps", [
    {"recoverable_requests": None}, {"recoverable_requests": True},
    {"recoverable_requests": 2}, {"daemon_instance": "invalid"},
    {"auth": "off"}, {"virtuoso_pid": True},
])
def test_unsupported_daemon_never_falls_back_to_legacy_execution(monkeypatch, caps):
    client, wire, _, _ = setup_client(monkeypatch)
    client._daemon_caps.update(caps)
    result = client.execute_skill("save()", timeout=5)
    assert result.metadata["outcome"] == "not_started"
    assert wire == []


@pytest.mark.parametrize("caps", [{"daemon_instance": "fe" * 16}, {"virtuoso_pid": 99}])
def test_daemon_restart_or_wrong_endpoint_cannot_recover_old_request(monkeypatch, caps):
    client, wire, _, _ = setup_client(monkeypatch)
    handle = RequestHandle(request_id="ef" * 16, daemon_instance=INSTANCE, virtuoso_pid=42)
    client._daemon_caps.update(caps)
    result = client.requests.receipt(handle)
    assert result.metadata["outcome"] == "unknown" and wire == []


@pytest.mark.parametrize("mutation", [
    {"request_id": "0" * 32}, {"daemon_instance": "fe" * 16},
    {"state": "completed", "response": None}, {"state": "completed", "response": "garbage"},
    {"state": "running", "response": "\x023"}, {"state": "unsupported"},
])
def test_malformed_or_misattributed_receipt_is_unknown(monkeypatch, mutation):
    client, _, _, _ = setup_client(monkeypatch)

    def corrupt(payload, deadline, **kwargs):
        data = {"request_id": payload["request_id"], "daemon_instance": INSTANCE, "state": "running"}
        data.update(mutation)
        body = json.dumps(data)
        mac = daemon_auth.response_mac(TOKEN, nonce=payload["nonce"], marker="\x02", body=body)
        return ("\x02" + mac + body).encode()

    monkeypatch.setattr(client, "_exchange_payload", corrupt)
    result = client.execute_skill("save()", timeout=5)
    assert result.metadata["outcome"] == "unknown"


def test_unsigned_receipt_is_not_trusted(monkeypatch):
    client, _, _, _ = setup_client(monkeypatch)
    monkeypatch.setattr(client, "_exchange_payload", lambda *a, **k: b'\x02{"state":"completed"}')
    result = client.execute_skill("save()", timeout=5)
    assert result.metadata["outcome"] == "unknown" and not result.ok


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_receipt_timeout_validation_before_network(monkeypatch, timeout):
    client, wire, _, _ = setup_client(monkeypatch)
    handle = RequestHandle(request_id="ef" * 16, daemon_instance=INSTANCE, virtuoso_pid=42)
    with pytest.raises(ValueError):
        client.requests.receipt(handle, timeout=timeout)
    assert wire == []


def test_protect_inflight_must_be_explicit_and_supported(monkeypatch):
    client, _, _, _ = setup_client(monkeypatch)
    client.dialogs.disable_guard()
    client.dialogs.enable_guard(protect_inflight=True)
    assert client.dialogs.protect_inflight
    client.dialogs.disable_guard()
    assert not client.dialogs.protect_inflight
    client.dialogs.enable_guard()
    assert client.dialogs.enabled and not client.dialogs.protect_inflight
    with pytest.raises(ValueError):
        client.dialogs.enable_guard(protect_inflight=1)


def test_old_daemon_refuses_activation_preserving_previous_binding(monkeypatch):
    client, _, _, _ = setup_client(monkeypatch)
    client.dialogs._protect_inflight = False
    client._daemon_caps.pop("recoverable_requests")
    original_target = client.dialogs.target
    with pytest.raises(ValueError, match="no legacy execution fallback"):
        client.dialogs.enable_guard(protect_inflight=True)
    assert client.dialogs.target == original_target and not client.dialogs.protect_inflight


@pytest.mark.parametrize("field", ["nonce", "op", "request_id", "daemon_instance", "skill", "proto"])
def test_recoverable_mac_covers_every_operation_field(field):
    fields = {"nonce": "ab" * 16, "op": "submit", "request_id": "ef" * 16,
              "daemon_instance": INSTANCE, "skill": "save()", "proto": 1}
    before = daemon_auth.recoverable_mac(TOKEN, **fields)
    fields[field] = 2 if field == "proto" else fields[field] + "x"
    assert daemon_auth.recoverable_mac(TOKEN, **fields) != before


def test_old_request_mac_cannot_authorize_recoverable_operation():
    old = daemon_auth.request_mac(TOKEN, nonce="ab" * 16, skill="save()", timeout=5)
    new = daemon_auth.recoverable_mac(TOKEN, nonce="ab" * 16, op="submit",
                                     request_id="ef" * 16, daemon_instance=INSTANCE, skill="save()")
    assert new != old


def test_maestro_error_keeps_lost_acknowledgement_handle(monkeypatch):
    client, wire, _, _ = setup_client(monkeypatch)

    def lost(payload, deadline, **kwargs):
        wire.append(payload)
        raise ConnectionResetError("acknowledgement lost")

    monkeypatch.setattr(client, "_exchange_payload", lost)
    with pytest.raises(RequestRecoveryError) as caught:
        writer._q(client, "save()")
    assert caught.value.handle.virtuoso_pid == 42
    assert caught.value.outcome == "unknown"
    assert len(wire) == 1


def test_maestro_start_keeps_callback_marker_on_uncertain_request(monkeypatch):
    from virtuoso_bridge.models import VirtuosoResult

    client = VirtuosoClient(daemon_token=TOKEN)
    handle = RequestHandle(request_id="ef" * 16, daemon_instance=INSTANCE, virtuoso_pid=42)
    calls = []

    def execute(code, **kwargs):
        calls.append(code)
        if len(calls) == 1:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")
        return client.requests._unknown(handle, "awaiting confirmation", phase="awaiting_user",
                                        request_sent=True)

    monkeypatch.setattr(client, "execute_skill", execute)
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: None)
    with pytest.raises(RequestRecoveryError) as caught:
        writer.run_and_wait(client, session="session1", timeout=5)
    assert caught.value.result.metadata["completion_marker"].startswith("/tmp/vb_sim_done_")
    assert caught.value.result.metadata["session"] == "session1"
    assert caught.value.result.metadata["maestro_phase"] == "simulation_start"
    assert caught.value.handle == handle
    assert sum("maeRunSimulation" in code for code in calls) == 1


def test_maestro_callback_uncertainty_does_not_claim_simulation_was_started(monkeypatch):
    client = VirtuosoClient(daemon_token=TOKEN)
    handle = RequestHandle(request_id="ef" * 16, daemon_instance=INSTANCE, virtuoso_pid=42)
    calls = []

    def execute(code, **kwargs):
        calls.append(code)
        return client.requests._unknown(handle, "callback acknowledgement lost", phase="submit")

    monkeypatch.setattr(client, "execute_skill", execute)
    monkeypatch.setattr(writer, "_remove_marker", lambda *a: None)
    with pytest.raises(RequestRecoveryError) as caught:
        writer.run_and_wait(client, session="session1", timeout=5)
    metadata = caught.value.result.metadata
    assert metadata["maestro_phase"] == "callback_setup"
    assert metadata["simulation_start_sent"] is False
    assert "completion_marker" not in metadata
    assert caught.value.handle == handle
    assert len(calls) == 1 and "maeRunSimulation" not in calls[0]


def test_completed_skill_error_is_not_success_and_retains_receipt(monkeypatch):
    client, _, _, _ = setup_client(monkeypatch)

    def failure(payload, deadline, **kwargs):
        body = json.dumps({"request_id": payload["request_id"], "daemon_instance": INSTANCE,
                           "state": "completed", "response": "\x15error in SKILL"})
        mac = daemon_auth.response_mac(TOKEN, nonce=payload["nonce"], marker="\x02", body=body)
        return ("\x02" + mac + body).encode()

    monkeypatch.setattr(client, "_exchange_payload", failure)
    result = client.execute_skill("bad()", timeout=5)
    assert not result.ok and result.errors == ["error in SKILL"]
    assert result.metadata["outcome"] == "completed"
    assert result.metadata["request_handle"]


def test_inspection_shares_remaining_budget(monkeypatch):
    client, wire, _, clock = setup_client(monkeypatch, ["running"])
    seen = []

    def inspect(**kwargs):
        seen.append(kwargs["timeout"])
        clock.sleep(kwargs["timeout"])
        return DialogInspection(status="clear", target=client.dialogs.target)

    monkeypatch.setattr(client.dialogs, "inspect", inspect)
    result = client.execute_skill("slow()", timeout=2)
    assert seen == [1.0]
    assert clock.now == 2
    assert result.metadata["phase"] == "wait_timeout"
    assert sum(p["op"] == "submit" for p in wire) == 1


def test_receipt_response_size_is_bounded(monkeypatch):
    import socket

    class LargeResponse:
        def __enter__(self):
            self.closed = False
            return self

        def __exit__(self, *args):
            self.closed = True

        def settimeout(self, seconds):
            assert seconds > 0

        def connect(self, endpoint):
            pass

        def sendall(self, data):
            pass

        def shutdown(self, how):
            pass

        def recv(self, size):
            return b"x" * 10

    transport = LargeResponse()
    monkeypatch.setattr(bridge_module.socket, "socket", lambda *a: transport)
    client = VirtuosoClient(daemon_token=TOKEN)
    with pytest.raises(OSError, match="size bound"):
        client._exchange_payload({"op": "receipt"}, requests_module.time.monotonic() + 5,
                                 max_response_bytes=15)
    assert transport.closed


@pytest.mark.parametrize("invalid", [
    {"request_id": "invalid"}, {"daemon_instance": "invalid"},
    {"virtuoso_pid": True}, {"virtuoso_pid": 0}, {"token": TOKEN},
])
def test_malformed_handle_is_rejected_before_any_query(monkeypatch, invalid):
    client, wire, _, _ = setup_client(monkeypatch)
    handle = {"request_id": "ef" * 16, "daemon_instance": INSTANCE, "virtuoso_pid": 42}
    handle.update(invalid)
    with pytest.raises(ValueError):
        client.requests.receipt(handle)
    assert wire == []


def test_request_id_encodes_creation_time_for_expiry_rejection(monkeypatch):
    client, wire, _, clock = setup_client(monkeypatch)
    assert client.execute_skill("1+2", timeout=5).ok
    assert int(wire[0]["request_id"][:12], 16) == int(clock.time() * 1000)


def test_request_id_prefers_signed_daemon_time_over_skewed_client_clock(monkeypatch):
    client, wire, _, clock = setup_client(monkeypatch)
    server_time = int((clock.time() - 86400) * 1000)
    client._daemon_caps["server_time_ms"] = server_time
    assert client.execute_skill("1+2", timeout=5).ok
    assert int(wire[0]["request_id"][:12], 16) == server_time


@pytest.mark.parametrize("server_time", [True, "1234", -1, 2**48])
def test_malformed_server_timestamp_never_sends_skill(monkeypatch, server_time):
    client, wire, _, _ = setup_client(monkeypatch)
    client._daemon_caps["server_time_ms"] = server_time
    result = client.execute_skill("save()", timeout=5)
    assert result.metadata["outcome"] == "not_started" and wire == []

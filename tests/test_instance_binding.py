"""Same-port replacement with shipped daemon handlers and real TCP sockets."""
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from virtuoso_bridge import VirtuosoClient, daemon_auth
from tests.test_instance_discovery import row
from tests.test_recoverable_daemon import TOKEN, _exchange, _import_daemon


class Endpoint:
    def __init__(self, module, port=0):
        self.module = module
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", port))
        self.socket.listen(8)
        self.socket.settimeout(0.05)
        self.port = self.socket.getsockname()[1]
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.serve)
        self.worker.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, address = self.socket.accept()
            except socket.timeout:
                continue
            connection.settimeout(2)
            self.module.handle_external_connection(connection, address)

    def close(self):
        self.stop.set()
        self.worker.join(3)
        self.socket.close()
        assert not self.worker.is_alive()


@pytest.fixture
def endpoints(monkeypatch, tmp_path):
    owned = []

    def launch(pid, port=0, token=TOKEN, registered=True):
        module = _import_daemon(monkeypatch, tmp_path)
        module.BRIDGE_TOKEN = token
        module.virtuoso_pid = pid
        executed = []
        monkeypatch.setattr(module, "_transmit_and_read",
                            lambda skill, timeout: executed.append(skill) or b"\x022")
        endpoint = Endpoint(module, port)
        owned.append(endpoint)
        record = row(endpoint.port, pid)
        if registered:
            module._INSTANCE_LEASE = SimpleNamespace(metadata=record)
        endpoint.record, endpoint.executed = record, executed
        return endpoint

    yield launch
    for endpoint in owned:
        endpoint.close()


def client_for(endpoint):
    client = VirtuosoClient(port=endpoint.port, daemon_token=TOKEN, log_to_ciw=False,
                           tunnel=SimpleNamespace(instance_record=endpoint.record))
    client.use_current_selection()
    return client


@pytest.mark.parametrize("fresh_client", [False, True])
@pytest.mark.parametrize("replacement_token", [TOKEN, "cd" * 32])
def test_same_port_replacement_never_executes(endpoints, fresh_client, replacement_token):
    original = endpoints(10)
    client = client_for(original)
    assert client.execute_skill("1+1", retry_connect=False).ok
    original.close()
    replacement = endpoints(20, original.port, replacement_token)
    if fresh_client:
        client = client_for(original)
    result = client.execute_skill("save()", retry_connect=False)
    assert not result.ok and "authentication" in result.errors[0].lower()
    assert original.executed == ["1+1"]
    assert replacement.executed == []
    assert client._tunnel.instance_record == original.record


def test_replacement_between_hello_and_execution_is_rejected(endpoints, monkeypatch):
    original = endpoints(10)
    client = client_for(original)
    exchange = client._exchange_payload
    replacements = []

    def swap(payload, deadline):
        reply = exchange(payload, deadline)
        if payload.get("op") == "hello":
            original.close()
            replacements.append(endpoints(20, original.port))
        return reply

    monkeypatch.setattr(client, "_exchange_payload", swap)
    assert not client.execute_skill("save()", retry_connect=False).ok
    assert original.executed == replacements[0].executed == []


def test_standalone_tool_binds_cached_capabilities(endpoints):
    from tools import skill_exec
    original = endpoints(10)
    caps, error = skill_exec.handshake("127.0.0.1", original.port, 3, TOKEN)
    assert error is None
    output, error = skill_exec.execute("1+1", port=original.port, token=TOKEN, caps=caps)
    assert error is None and output == "2"
    original.close()
    replacement = endpoints(20, original.port)
    output, error = skill_exec.execute("save()", port=original.port, token=TOKEN, caps=caps)
    assert output is None and "daemon_instance mismatch" in error
    assert replacement.executed == []


@pytest.mark.parametrize("changes", [
    {"instance_bound_requests": None}, {"instance_bound_requests": True},
    {"instance_bound_requests": 0}, {"instance_bound_requests": "1"},
    {"daemon_instance": None}, {"daemon_instance": "invalid"},
])
def test_registered_peer_without_valid_binding_never_gets_skill(endpoints, monkeypatch, changes):
    endpoint = endpoints(10)
    caps = json.loads(endpoint.module._capabilities_body())
    caps.update(changes)
    monkeypatch.setattr(endpoint.module, "_capabilities_body", lambda: json.dumps(caps))
    assert not client_for(endpoint).execute_skill("save()", retry_connect=False).ok
    from tools import skill_exec
    output, error = skill_exec.execute("save()", port=endpoint.port, token=TOKEN)
    assert output is None and error
    assert endpoint.executed == []


@pytest.mark.parametrize("binding", [True, "1", 2, [], {}])
def test_malformed_binding_cannot_select_legacy_fallback(endpoints, monkeypatch, binding):
    endpoint = endpoints(10, registered=False)
    caps = json.loads(endpoint.module._capabilities_body())
    caps["instance_bound_requests"] = binding
    monkeypatch.setattr(endpoint.module, "_capabilities_body", lambda: json.dumps(caps))
    client = VirtuosoClient(port=endpoint.port, daemon_token=TOKEN, log_to_ciw=False)
    assert not client.execute_skill("save()", retry_connect=False).ok
    from tools import skill_exec
    output, error = skill_exec.execute("save()", port=endpoint.port, token=TOKEN)
    assert output is None and error
    assert endpoint.executed == []


@pytest.mark.parametrize("field,value", [
    ("proto", 2), ("nonce", "cd" * 16), ("daemon_instance", "ef" * 16),
    ("skill", "save()"), ("timeout", 9), ("op", "submit"), ("op", None),
])
def test_execution_binding_authenticates_every_field(endpoints, field, value):
    endpoint = endpoints(10)
    request = dict(proto=1, nonce="ab" * 16, op="execute", skill="1+1", timeout=1,
                   daemon_instance=endpoint.module._RECOVERY.instance_id)
    request["mac"] = daemon_auth.bound_request_mac(
        TOKEN, **{key: request[key] for key in ("proto", "nonce", "skill", "timeout", "daemon_instance")})
    tampered = dict(request, **{field: value})
    assert b"AuthError" in _exchange(endpoint.module, tampered)
    assert endpoint.executed == []
    assert _exchange(endpoint.module, request).startswith(b"\x02")
    assert endpoint.executed == ["1+1"]
    assert b"replayed" in _exchange(endpoint.module, request)


@pytest.mark.parametrize("registered", [False, True])
def test_legacy_request_only_accepted_by_unregistered_daemon(endpoints, registered):
    endpoint = endpoints(10, registered=registered)
    payload = dict(proto=1, nonce="ab" * 16, skill="1+1", timeout=1)
    payload["mac"] = daemon_auth.request_mac(TOKEN, **payload)
    reply = _exchange(endpoint.module, payload)
    assert reply.startswith(b"\x15AuthError" if registered else b"\x02")
    assert endpoint.executed == ([] if registered else ["1+1"])

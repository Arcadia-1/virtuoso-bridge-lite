"""Tests for SSH-bootstrapped bridge token authentication (daemon_auth).

Covers the HMAC wire protocol v1 in both directions — full-coverage request
and response MACs over canonical frames, the side-effect-free capability
handshake, server-side replay protection, the atomic 0600 token storage, and
the fail-fatal provisioning policy — plus explicitly that the explicit
insecure opt-in still works end to end.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from virtuoso_bridge import daemon_auth
from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

STX, NAK = "\x02", "\x15"
TOKEN = "ab" * 32
WRONG_TOKEN = "cd" * 32


# ---------------------------------------------------------------------------
# Wire-level fake daemons implementing the protocol v1 auth contract
# ---------------------------------------------------------------------------


class _AuthDaemon:
    """RAMIC-wire daemon implementing the token auth contract.

    token=None models an explicitly auth-disabled daemon (protocol v1, but
    no signing and no request checks).  ``legacy=True`` models a pre-token
    daemon that ignores the auth fields entirely — and therefore fails on
    the handshake's missing ``skill`` key exactly like the real old code.
    """

    def __init__(self, token: str | None = TOKEN, legacy: bool = False,
                 caps_body: str | None = None):
        self.token = None if legacy else token
        self.legacy = legacy
        # When set, the handshake answers with this raw payload instead of
        # the canonical caps JSON (for malformed-payload regression tests).
        self.caps_body = caps_body
        self.requests: list[dict] = []  # received (pre-auth)
        self.executed: list[str] = []  # post-auth SKILL executions
        self.virtuoso_pid = 4242
        self._seen_nonces: set[str] = set()
        self._seen_lock = threading.Lock()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _nak(self, conn: socket.socket, message: str) -> None:
        conn.sendall((NAK + message).encode("utf-8"))

    def _check_request(self, req: dict) -> str | None:
        """Auth check mirroring the real daemon (regular requests)."""
        if self.token is None:
            return None
        nonce, mac = req.get("nonce"), req.get("mac")
        if not nonce or not mac:
            return "AuthError: bridge token required"
        key = str(nonce)
        with self._seen_lock:
            if key in self._seen_nonces:
                return "AuthError: replayed request nonce"
            self._seen_nonces.add(key)
        expected = daemon_auth.request_mac(
            self.token,
            nonce=req["nonce"],
            skill=req["skill"],
            timeout=req["timeout"],
            proto=req.get("proto", 0),
        )
        if expected != str(mac).lower():
            return "AuthError: bridge token mismatch"
        return None

    def _sign(self, req: dict, marker: str, body: str) -> str:
        return daemon_auth.response_mac(
            self.token, nonce=str(req["nonce"]), marker=marker, body=body.encode("utf-8")
        )

    def _handle(self, conn: socket.socket) -> None:
        try:
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            req = json.loads(b"".join(chunks).decode("utf-8"))
            self.requests.append(req)

            # Capability handshake: no "skill" field, nothing executes.
            if req.get("op") == "hello":
                caps = self.caps_body if self.caps_body is not None else json.dumps(
                    {
                        "proto": 1,
                        "auth": "on" if self.token else "off",
                        "daemon": "fake-bridge",
                        "virtuoso_pid": self.virtuoso_pid,
                    }
                )
                if self.legacy:
                    # Real pre-token daemons do request_data["skill"] and
                    # die with an unauthenticated KeyError NAK.
                    self._nak(conn, "'skill'")
                    return
                if self.token:
                    nonce, mac = req.get("nonce"), req.get("mac")
                    if not nonce or not mac:
                        self._nak(conn, "AuthError: bridge token required")
                        return
                    expected = daemon_auth.hello_mac(self.token, nonce=nonce)
                    if expected != str(mac).lower():
                        self._nak(conn, "AuthError: bridge token mismatch")
                        return
                    conn.sendall(
                        (STX + self._sign(req, STX, caps) + caps).encode("utf-8")
                    )
                else:
                    conn.sendall((STX + caps).encode("utf-8"))
                return

            auth_error = self._check_request(req)
            if auth_error:
                self._nak(conn, auth_error)
                return

            skill = req["skill"]
            self.executed.append(skill)
            if skill.strip() == "getpid()":
                body = str(self.virtuoso_pid)
            else:
                body = '"2"' if skill.strip() == "1+1" else "nil"
            if self.token and req.get("nonce"):
                conn.sendall((STX + self._sign(req, STX, body) + body).encode("utf-8"))
            else:
                conn.sendall((STX + body).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            try:
                conn.sendall((NAK + str(exc)).encode("utf-8"))
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._sock.close()


@pytest.fixture()
def authed_daemon():
    daemon = _AuthDaemon(token=TOKEN)
    yield daemon
    daemon.close()


@pytest.fixture()
def legacy_daemon():
    daemon = _AuthDaemon(legacy=True)  # pre-token-auth daemon
    yield daemon
    daemon.close()


@pytest.fixture()
def authoff_daemon():
    daemon = _AuthDaemon(token=None)  # explicit RB_ALLOW_UNAUTHENTICATED daemon
    yield daemon
    daemon.close()


@pytest.fixture()
def token_home(monkeypatch, tmp_path):
    """Isolated HOME so tests never touch the real ~/.virtuoso-bridge."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv(daemon_auth.UNAUTH_OPTIN_ENV, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Canonical frames and MACs
# ---------------------------------------------------------------------------


def test_request_mac_covers_every_field() -> None:
    base = dict(nonce="12" * 16, skill="1+1", timeout=5.0)
    reference = daemon_auth.request_mac(TOKEN, **base)
    assert daemon_auth.request_mac(TOKEN, **base) == reference
    # Swapping any single authenticated field must change the MAC.
    assert daemon_auth.request_mac(TOKEN, nonce="34" * 16, **{k: v for k, v in base.items() if k != "nonce"}) != reference
    assert daemon_auth.request_mac(TOKEN, skill="1+2", timeout=5.0, nonce=base["nonce"]) != reference
    assert daemon_auth.request_mac(TOKEN, nonce=base["nonce"], skill="1+1", timeout=6.0) != reference
    assert daemon_auth.request_mac(WRONG_TOKEN, **base) != reference


def test_canonical_frame_is_length_prefixed_and_unambiguous() -> None:
    a = daemon_auth.canonical_frame("ab", "c")
    b = daemon_auth.canonical_frame("a", "bc")
    assert a != b  # naive concatenation would collide
    assert daemon_auth.canonical_frame("x\ny", "z") == b"3:x\ny1:z"


def test_sign_and_verify_roundtrip() -> None:
    nonce = "12" * 16
    raw = STX + daemon_auth.response_mac(TOKEN, nonce=nonce, marker=STX, body="hello") + "hello"
    assert daemon_auth.verify_response(raw, TOKEN, nonce) == STX + "hello"


def test_verify_rejects_tampered_body() -> None:
    nonce = "12" * 16
    mac = daemon_auth.response_mac(TOKEN, nonce=nonce, marker=STX, body="hello")
    raw = STX + mac + "hello-tampered"
    with pytest.raises(daemon_auth.DaemonAuthError, match="spoofed listener"):
        daemon_auth.verify_response(raw, TOKEN, nonce)


def test_verify_rejects_wrong_token() -> None:
    nonce = "12" * 16
    raw = STX + daemon_auth.response_mac(TOKEN, nonce=nonce, marker=STX, body="hello") + "hello"
    with pytest.raises(daemon_auth.DaemonAuthError, match="spoofed listener"):
        daemon_auth.verify_response(raw, WRONG_TOKEN, nonce)


def test_verify_rejects_missing_mac() -> None:
    with pytest.raises(daemon_auth.DaemonAuthError, match="restart"):
        daemon_auth.verify_response(NAK + "TimeoutError", TOKEN, "12" * 16)


def test_verify_rejects_non_hex_prefix() -> None:
    with pytest.raises(daemon_auth.DaemonAuthError, match="restart"):
        daemon_auth.verify_response(STX + "zz" * 32 + "body", TOKEN, "12" * 16)


# ---------------------------------------------------------------------------
# Token storage: atomic 0600 file under a 0700 directory
# ---------------------------------------------------------------------------


def test_token_file_is_atomic_0600_under_0700(token_home) -> None:
    token = daemon_auth.read_or_create_local_token()
    assert daemon_auth.is_valid_token(token)
    path = daemon_auth.token_path()
    if os.name != "nt":  # chmod is advisory on Windows
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    # Atomic write leaves no temp files behind.
    assert [entry.name for entry in path.parent.iterdir()] == ["bridge_token"]
    # Idempotent: rewriting must not rotate the token.
    assert daemon_auth.read_or_create_local_token() == token


def test_read_local_token_never_creates(token_home) -> None:
    assert daemon_auth.read_local_token() is None
    assert not daemon_auth.token_path().exists()


def test_local_token_or_raise_is_fatal_without_optin(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv(daemon_auth.TOKEN_PATH_ENV, str(blocker / "sub" / "token"))
    with pytest.raises(daemon_auth.DaemonTokenError, match="explicitly"):
        daemon_auth.local_token_or_raise()


def test_local_token_or_raise_optin_returns_none(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv(daemon_auth.TOKEN_PATH_ENV, str(blocker / "sub" / "token"))
    monkeypatch.setenv(daemon_auth.UNAUTH_OPTIN_ENV, "1")
    assert daemon_auth.local_token_or_raise() is None


def test_save_state_omits_daemon_token(tmp_path, monkeypatch) -> None:
    """The token must never be written to the normally readable state.json."""
    monkeypatch.setenv("VB_STATE_DIR", str(tmp_path / "state"))
    from virtuoso_bridge.transport.tunnel import SSHClient

    client = SSHClient(remote_host="localhost", port=65432)
    client._daemon_token = TOKEN
    client.save_state()
    raw = (tmp_path / "state" / "state.json").read_text(encoding="utf-8")
    assert "daemon_token" not in json.loads(raw)
    assert TOKEN not in raw


# ---------------------------------------------------------------------------
# Client <-> daemon over the wire
# ---------------------------------------------------------------------------


def test_client_with_token_executes(authed_daemon) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port, daemon_token=TOKEN)
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.SUCCESS
    assert result.output == '"2"'
    request = authed_daemon.requests[-1]
    assert daemon_auth.request_mac(
        TOKEN, nonce=request["nonce"], skill=request["skill"],
        timeout=request["timeout"], proto=request["proto"],
    ) == request["mac"]


def test_handshake_runs_once_and_is_side_effect_free(authed_daemon) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port, daemon_token=TOKEN)
    assert client.execute_skill("1+1", timeout=5).status is ExecutionStatus.SUCCESS
    assert client.execute_skill("1+2", timeout=5).status is ExecutionStatus.SUCCESS
    hellos = [r for r in authed_daemon.requests if r.get("op") == "hello"]
    assert len(hellos) == 1  # cached after the first exchange
    # No capability exchange ever carries executable content.
    assert all("skill" not in r for r in hellos)


def test_handshake_signed_with_hello_domain(authed_daemon) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port, daemon_token=TOKEN)
    client.execute_skill("1+1", timeout=5)
    hello = next(r for r in authed_daemon.requests if r.get("op") == "hello")
    assert daemon_auth.hello_mac(TOKEN, nonce=hello["nonce"]) == hello["mac"]


def test_client_with_wrong_token_is_rejected_before_execution(authed_daemon) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port, daemon_token=WRONG_TOKEN)
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.ERROR
    assert "Bridge authentication failed" in result.errors[0]
    assert "token mismatch" in result.errors[0]
    # The foreign daemon must not have executed anything.
    assert authed_daemon.executed == []


def test_client_refuses_pre_token_daemon_before_any_execution(legacy_daemon) -> None:
    """Protocol skew must be detected by the handshake, BEFORE the command.

    The pre-token daemon ignores auth fields and would happily execute the
    SKILL and return an unsigned response; the handshake's skill-less hello
    makes it fail with an unauthenticated KeyError NAK instead.
    """
    client = VirtuosoClient(host="127.0.0.1", port=legacy_daemon.port, daemon_token=TOKEN)
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.ERROR
    assert "capability handshake" in result.errors[0]
    assert legacy_daemon.executed == []  # nothing ran on the old daemon
    # Only the skill-less hello reached it.
    assert all("skill" not in r for r in legacy_daemon.requests)
    assert legacy_daemon.requests  # the handshake itself did go out


def test_legacy_client_is_rejected_by_authed_daemon(authed_daemon) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port)  # no token
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.ERROR
    assert "bridge token required" in result.errors[0]
    assert authed_daemon.executed == []


def test_token_client_refuses_auth_off_daemon(authoff_daemon) -> None:
    """An auth-disabled daemon cannot sign anything; refuse unless opted in."""
    client = VirtuosoClient(host="127.0.0.1", port=authoff_daemon.port, daemon_token=TOKEN)
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.ERROR
    assert "token authentication DISABLED" in result.errors[0]
    assert authoff_daemon.executed == []


def test_optin_client_talks_to_auth_off_daemon(token_home, authoff_daemon, monkeypatch) -> None:
    monkeypatch.setenv(daemon_auth.UNAUTH_OPTIN_ENV, "1")
    client = VirtuosoClient(host="127.0.0.1", port=authoff_daemon.port, daemon_token=None)
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.SUCCESS
    assert result.output == '"2"'
    assert authoff_daemon.executed == ["1+1"]


def test_bare_client_requires_optin_for_auth_off_daemon(token_home, authoff_daemon) -> None:
    """A directly constructed (token-less) client is NOT a license to run
    unauthenticated: an auth-disabled daemon still requires the explicit
    client-side opt-in."""
    client = VirtuosoClient(host="127.0.0.1", port=authoff_daemon.port, daemon_token=None)
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.ERROR
    assert "explicitly accept insecure legacy mode" in result.errors[0]
    assert authoff_daemon.executed == []


@pytest.mark.parametrize(
    "caps_body",
    [
        "null",                       # valid JSON, wrong type
        "[1, 2]",                     # valid JSON, wrong type
        '{"proto": 1}',               # missing auth
        '{"proto": 1, "auth": "maybe"}',  # bad auth flag
        '{"proto": "1", "auth": "on"}',   # wrong proto type
        '{"proto": 1, "auth": "on", "virtuoso_pid": "4242"}',  # bad pid type
        '{"proto": 1, "auth": "on", "virtuoso_pid": -5}',      # bad pid value
        "not json at all",
    ],
)
def test_malformed_capability_payloads_are_refused(token_home, caps_body) -> None:
    daemon = _AuthDaemon(token=TOKEN, caps_body=caps_body)
    try:
        client = VirtuosoClient(host="127.0.0.1", port=daemon.port, daemon_token=TOKEN)
        result = client.execute_skill("1+1", timeout=5)
        assert result.status is ExecutionStatus.ERROR
        assert (
            "malformed capability payload" in result.errors[0]
            or "unparseable capability payload" in result.errors[0]
            or "protocol version mismatch" in result.errors[0]
        )
        assert daemon.executed == []
    finally:
        daemon.close()


def test_token_race_adopts_disk_token(token_home, monkeypatch) -> None:
    """First-time creation race: the on-disk file (the racing winner's) is
    authoritative — clients must converge on one secret, never diverge."""
    winner_token = "ef" * 32

    def lost_race(target, token):
        from virtuoso_bridge.daemon_auth import _tighten_perms  # type: ignore[attr-defined]
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(winner_token + "\n", encoding="utf-8")
        _tighten_perms(target)
        return winner_token

    monkeypatch.setattr(daemon_auth, "create_token_exclusive", lost_race)
    assert daemon_auth.read_or_create_local_token() == winner_token
    assert daemon_auth.read_local_token() == winner_token


def test_token_creation_fails_closed_without_exclusive_link(
    token_home,
    monkeypatch,
) -> None:
    def unsupported_link(_source, _target):
        raise OSError(errno.EPERM, "hard links unavailable")

    monkeypatch.setattr(os, "link", unsupported_link)

    assert daemon_auth.read_or_create_local_token() is None
    assert daemon_auth.read_local_token() is None


def test_server_rejects_replayed_nonce(authed_daemon) -> None:
    """Even a validly signed request must not replay: nonce is single-use."""
    payload = {
        "proto": 1,
        "nonce": "34" * 16,
        "skill": "1+1",
        "timeout": 5,
        "mac": daemon_auth.request_mac(
            TOKEN, nonce="34" * 16, skill="1+1", timeout=5.0
        ),
    }
    raw = json.dumps(payload).encode("utf-8")

    def exchange() -> str:
        s = socket.create_connection(("127.0.0.1", authed_daemon.port), timeout=5)
        s.sendall(raw)
        s.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        return data.decode("utf-8")

    first = exchange()
    assert first.startswith(STX)
    second = exchange()
    assert second.startswith(NAK)
    assert "replayed" in second


# ---------------------------------------------------------------------------
# Non-SSH access methods stay functional
# ---------------------------------------------------------------------------


def test_local_mode_client_works_without_ssh(token_home, authed_daemon) -> None:
    """VirtuosoClient.local() provisions the same token file the daemon uses."""
    client = VirtuosoClient.local(port=authed_daemon.port, timeout=5)
    assert daemon_auth.is_valid_token(client.daemon_token)
    token_file = Path(token_home, ".virtuoso-bridge", "bridge_token")
    assert client.daemon_token == token_file.read_text().strip().lower()
    # The daemon reads the same file (read-or-create), so it holds the same
    # secret — model that by rebuilding the fixture's daemon on this token.
    authed_daemon.token = client.daemon_token
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.SUCCESS
    assert result.output == '"2"'


def test_local_mode_token_failure_is_fatal(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv(daemon_auth.TOKEN_PATH_ENV, str(blocker / "sub" / "token"))
    with pytest.raises(daemon_auth.DaemonTokenError):
        VirtuosoClient.local(port=1, timeout=5)


def test_from_env_without_tunnel_defers_daemon_auth(monkeypatch) -> None:
    class _NoTunnelSSHClient:
        port = 65001

        @staticmethod
        def is_running(profile=None):
            return False

        @classmethod
        def from_env(cls, keep_remote_files=True, profile=None):
            assert keep_remote_files
            return cls()

    monkeypatch.setattr(
        "virtuoso_bridge.virtuoso.basic.bridge.load_vb_env",
        lambda: None,
    )
    monkeypatch.setattr(
        "virtuoso_bridge.transport.tunnel.SSHClient",
        _NoTunnelSSHClient,
    )
    monkeypatch.setattr(
        "virtuoso_bridge.virtuoso.basic.bridge._acquire_daemon_token",
        lambda _ssh: pytest.fail("daemon token was acquired before tunnel startup"),
    )
    monkeypatch.setattr(
        VirtuosoClient,
        "_reject_cross_user_daemon_if_reachable",
        lambda *_args, **_kwargs: pytest.fail("daemon was probed before tunnel startup"),
    )

    client = VirtuosoClient.from_env()

    assert client.port == 65001
    assert client.daemon_token is None


def test_skill_exec_tool_signs_and_verifies(authed_daemon, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    token_file = tmp_path / "bridge_token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")

    caps, error = tool.handshake(
        "127.0.0.1", authed_daemon.port, 5, tool._load_token(str(token_file))
    )
    assert error is None and caps["proto"] == 1 and caps["auth"] == "on"
    result, error = tool.execute(
        "1+1", host="127.0.0.1", port=authed_daemon.port, timeout=5,
        token=tool._load_token(str(token_file)), caps=caps,
    )
    assert error is None and result == '"2"'


def test_skill_exec_tool_rejected_without_token(authed_daemon) -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    caps, error = tool.handshake("127.0.0.1", authed_daemon.port, 5, None)
    assert caps is None and "AuthError" in error


def test_skill_exec_handshake_parses_unsigned_caps(authoff_daemon) -> None:
    """--no-token handshake against an auth-disabled daemon must parse the
    unsigned capability payload (marker stripped before json.loads)."""
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    caps, error = tool.handshake("127.0.0.1", authoff_daemon.port, 5, None)
    assert error is None
    assert caps["proto"] == 1 and caps["auth"] == "off"


@pytest.mark.parametrize(
    "caps_body",
    [
        "null",
        '{"proto": 2, "auth": "off"}',
        '{"proto": 1, "auth": "on"}',
        '{"proto": 1, "auth": "off", "virtuoso_pid": "4242"}',
    ],
)
def test_skill_exec_handshake_rejects_bad_unsigned_caps(caps_body) -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    daemon = _AuthDaemon(token=None, caps_body=caps_body)
    try:
        caps, error = tool.handshake("127.0.0.1", daemon.port, 5, None)
        assert caps is None
        assert error
        assert daemon.executed == []
    finally:
        daemon.close()


def test_skill_exec_rejects_unvalidated_supplied_caps(authoff_daemon) -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    result, error = tool.execute(
        "1+1",
        host="127.0.0.1",
        port=authoff_daemon.port,
        timeout=5,
        token=None,
        caps={},
    )

    assert result is None and error
    assert authoff_daemon.executed == []


def test_skill_exec_cli_missing_token_fails_closed(monkeypatch, capsys) -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(sys, "argv", ["skill_exec.py", "1+1"])
    monkeypatch.setattr(tool, "_load_token", lambda _path: None)

    assert tool.main() == 1
    assert "refusing to send SKILL unauthenticated" in capsys.readouterr().err


def test_skill_exec_execute_handshakes_itself(legacy_daemon) -> None:
    """execute() must never skip the handshake: an old daemon is detected
    (and executes nothing) even when callers pass no caps payload."""
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    result, error = tool.execute(
        "1+1", host="127.0.0.1", port=legacy_daemon.port, timeout=5, token=TOKEN
    )
    assert result is None and "capability handshake" in error
    assert legacy_daemon.executed == []


def test_skill_exec_execute_optin_against_auth_off_daemon(
    token_home, authoff_daemon, monkeypatch
) -> None:
    monkeypatch.setenv(daemon_auth.UNAUTH_OPTIN_ENV, "1")
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    result, error = tool.execute(
        "1+1", host="127.0.0.1", port=authoff_daemon.port, timeout=5, token=None
    )
    assert error is None and result == '"2"'
    assert authoff_daemon.executed == ["1+1"]


# ---------------------------------------------------------------------------
# Real daemon script (macOS/Linux only: POSIX fcntl + signals)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _resources_dir() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "src" / "virtuoso_bridge" / "virtuoso" / "basic" / "resources"
    )


def _raw_exchange(port: int, payload: dict) -> str:
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(json.dumps(payload).encode())
    s.shutdown(socket.SHUT_WR)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    return data.decode("utf-8", errors="replace")


def _wait_for_listener(proc: subprocess.Popen, port: int) -> None:
    import time

    for _ in range(100):
        try:
            probe = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            probe.close()
            return
        except OSError:
            if proc.poll() is not None:
                pytest.fail("daemon exited early")
            time.sleep(0.05)
    pytest.fail("daemon never started listening")


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="real bridge daemon needs POSIX fcntl/signals (macOS/Linux only)",
)
class TestRealDaemon:
    def test_rejects_unauthenticated_and_mismatched_requests(self, tmp_path) -> None:
        daemon_src = _resources_dir() / "ramic_bridge_daemon_3.py"
        token_path = tmp_path / "bridge_token"
        port = _free_port()
        env = dict(os.environ, RB_TOKEN_PATH=str(token_path))
        proc = subprocess.Popen(
            [sys.executable, str(daemon_src), "127.0.0.1", str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=env,
        )
        try:
            _wait_for_listener(proc, port)
            assert token_path.exists(), "daemon must auto-provision its token file"
            if os.name != "nt":
                assert token_path.stat().st_mode & 0o777 == 0o600

            reply = _raw_exchange(port, {"skill": "1+1", "timeout": 1})
            assert reply.startswith(NAK) and "bridge token required" in reply

            nonce = "34" * 16
            reply = _raw_exchange(
                port,
                {"skill": "1+1", "timeout": 1, "nonce": nonce,
                 "mac": daemon_auth.request_mac(
                     WRONG_TOKEN, nonce=nonce, skill="1+1", timeout=1.0)},
            )
            assert reply.startswith(NAK) and "token mismatch" in reply
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_signed_handshake_and_replay_protection(self, tmp_path) -> None:
        daemon_src = _resources_dir() / "ramic_bridge_daemon_3.py"
        token_path = tmp_path / "bridge_token"
        port = _free_port()
        env = dict(os.environ, RB_TOKEN_PATH=str(token_path))
        proc = subprocess.Popen(
            [sys.executable, str(daemon_src), "127.0.0.1", str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=env,
        )
        try:
            _wait_for_listener(proc, port)
            token = daemon_auth.read_local_token(token_path)
            assert token, "provisioned token must be readable"

            # Signed hello -> signed capability payload, nothing executed.
            nonce = "11" * 16
            reply = _raw_exchange(
                port,
                {"proto": 1, "nonce": nonce, "op": "hello",
                 "mac": daemon_auth.hello_mac(token, nonce=nonce)},
            )
            assert reply.startswith(STX)
            body = json.loads(daemon_auth.verify_response(reply, token, nonce)[1:])
            assert body["proto"] == 1 and body["auth"] == "on"
            assert isinstance(body.get("virtuoso_pid"), int)

            # Validly signed request accepted; the identical replay is
            # rejected by server-side nonce tracking even though the MAC
            # is still valid.
            signed = {
                "proto": 1, "skill": "1+1", "timeout": 1, "nonce": "22" * 16,
                "mac": daemon_auth.request_mac(
                    token, nonce="22" * 16, skill="1+1", timeout=1.0
                ),
            }
            first = _raw_exchange(port, signed)
            assert first.startswith(NAK)  # no Virtuoso behind stdin -> timeout NAK
            # ...but it IS the daemon's signed NAK, not a rejection.
            assert "AuthError" not in first
            daemon_auth.verify_response_bytes(
                first.encode("utf-8"), token, "22" * 16
            )
            second = _raw_exchange(port, signed)
            assert second.startswith(NAK) and "replayed" in second
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_missing_token_is_fatal_unless_explicitly_opted_in(self, tmp_path) -> None:
        daemon_src = _resources_dir() / "ramic_bridge_daemon_3.py"
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        port = _free_port()

        # Default: refuse to serve without a token.
        env = dict(os.environ, RB_TOKEN_PATH=str(blocker / "sub" / "token"))
        proc = subprocess.Popen(
            [sys.executable, str(daemon_src), "127.0.0.1", str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=env,
        )
        try:
            assert proc.wait(timeout=10) != 0
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("daemon must exit when it cannot provision a token")

        # Explicit opt-in: boots with auth disabled.
        env = dict(
            os.environ,
            RB_TOKEN_PATH=str(blocker / "sub" / "token"),
            **{daemon_auth.DAEMON_UNAUTH_OPTIN_ENV: "1"},
        )
        proc = subprocess.Popen(
            [sys.executable, str(daemon_src), "127.0.0.1", str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=env,
        )
        try:
            _wait_for_listener(proc, port)
            caps = json.loads(_raw_exchange(port, {"proto": 1, "op": "hello"})[1:])
            assert caps["auth"] == "off"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_daemon_scripts_compile() -> None:
    resources = _resources_dir()
    for name in ("ramic_bridge_daemon_3.py", "ramic_bridge_daemon_27.py"):
        rc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(resources / name)],
            capture_output=True,
        )
        assert rc.returncode == 0, rc.stderr.decode()


def test_py27_daemon_source_is_pure_ascii() -> None:
    """Python 2.7 parses ASCII-only sources without a coding cookie.

    One stray em-dash makes the py2 daemon die with SyntaxError before it
    can serve anything, so this is an encoding regression guard.
    """
    data = (_resources_dir() / "ramic_bridge_daemon_27.py").read_bytes()
    data.decode("ascii")


def test_py27_daemon_has_constant_time_compare_fallback() -> None:
    """hmac.compare_digest only exists from python2.7.7; the py2 daemon must
    ship a constant-time fallback for older 2.7 patch releases."""
    src = (_resources_dir() / "ramic_bridge_daemon_27.py").read_text(encoding="ascii")
    assert "_compare_digest = _hmac.compare_digest" in src
    assert "except AttributeError" in src
    assert "_compare_digest(expected" in src  # the verification path uses it
    assert "_hmac.compare_digest(expected" not in src  # no unguarded call



# ---------------------------------------------------------------------------
# Real daemon module (imported directly; fcntl stubbed for Windows)
# ---------------------------------------------------------------------------


def _import_py3_daemon(monkeypatch, tmp_path):
    """Import the actual ramic_bridge_daemon_3.py with fcntl/stdin stubbed."""
    import types

    fcntl_stub = types.SimpleNamespace(
        fcntl=lambda *args, **kwargs: 0, F_GETFL=3, F_SETFL=4
    )
    monkeypatch.setitem(sys.modules, "fcntl", fcntl_stub)
    # pytest's captured stdin has no fileno(); the daemon only needs one at
    # import time (non-blocking setup), never during these checks.
    stdin_stub = types.SimpleNamespace(
        fileno=lambda: 0,
        buffer=types.SimpleNamespace(read=lambda n=1: b""),
    )
    monkeypatch.setattr(sys, "stdin", stdin_stub)
    token_file = tmp_path / "bridge_token"
    monkeypatch.setenv("RB_TOKEN_PATH", str(token_file))
    monkeypatch.setattr(sys, "argv", ["daemon", "127.0.0.1", "1"])
    spec = importlib.util.spec_from_file_location(
        "vb_real_daemon_under_test", str(_resources_dir() / "ramic_bridge_daemon_3.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_daemon_module_interops_with_client_macs(tmp_path, monkeypatch) -> None:
    """The shipped daemon accepts exactly the client's canonical signatures."""
    module = _import_py3_daemon(monkeypatch, tmp_path)
    token = daemon_auth.read_local_token(module._token_file_path())
    assert daemon_auth.is_valid_token(token)

    # TAMPER: a valid nonce+mac over a DIFFERENT skill must be rejected and
    # must not burn the nonce.
    nonce = "ab" * 16
    signed = {
        "proto": 1, "nonce": nonce, "skill": "1+1", "timeout": 2.5,
        "mac": daemon_auth.request_mac(
            token, nonce=nonce, skill="1+1", timeout=2.5
        ),
    }
    tampered = dict(signed, skill="deleteWindow()")
    error = module._auth_error(tampered, "req")
    assert error and "token mismatch" in error

    # The untampered request passes every check.
    assert module._auth_error(signed, "req") is None
    # ...and its nonce is now single-use (server-side replay protection).
    replay = module._auth_error(dict(signed), "req")
    assert replay and "replayed" in replay

    # Signed hello: accepted, capabilities signable, signature verifies with
    # the client's own verify function.
    hello_nonce = "cd" * 16
    hello = {
        "proto": 1, "nonce": hello_nonce, "op": "hello",
        "mac": daemon_auth.hello_mac(token, nonce=hello_nonce),
    }
    assert module._auth_error(hello, "hello") is None
    caps_body = module._capabilities_body().encode("utf-8")
    caps_mac = module._mac_hex(
        module._RESP_DOMAIN, hello_nonce, "\x02", caps_body
    )
    verified = daemon_auth.verify_response_bytes(
        b"\x02" + caps_mac.encode("ascii") + caps_body, token, hello_nonce
    )
    caps = json.loads(verified[1:].decode("utf-8"))
    assert caps["proto"] == 1 and caps["auth"] == "on"

    # Full-response MAC covers marker AND body: flipping a body byte breaks it.
    bad_body = b"\x02" + caps_mac.encode("ascii") + caps_body[:-1] + b"!"
    with pytest.raises(daemon_auth.DaemonAuthError):
        daemon_auth.verify_response_bytes(bad_body, token, hello_nonce)


def test_real_daemon_module_skill_field_authenticates_body(tmp_path, monkeypatch) -> None:
    """Multi-byte payloads: the MAC's length prefixes must survive embedding."""
    module = _import_py3_daemon(monkeypatch, tmp_path)
    token = daemon_auth.read_local_token(module._token_file_path())
    skill = 'let((x) x="1:2 abc" printf("%s" x) x)'  # contains frame-like text
    nonce = "ef" * 16
    request = {
        "proto": 1, "nonce": nonce, "skill": skill, "timeout": 0.25,
        "mac": daemon_auth.request_mac(
            token, nonce=nonce, skill=skill, timeout=0.25
        ),
    }
    assert module._auth_error(request, "req") is None


def test_real_daemon_nonce_cache_fails_closed_at_capacity(
    tmp_path, monkeypatch
) -> None:
    """At capacity the cache must NEVER drop live entries: new requests are
    rejected outright (fail-closed) and existing nonces still replay-fail."""
    module = _import_py3_daemon(monkeypatch, tmp_path)
    token = daemon_auth.read_local_token(module._token_file_path())
    live = {"nonce": "12" * 16, "skill": "1+1", "timeout": 5, "proto": 1}
    live["mac"] = daemon_auth.request_mac(
        token, nonce=live["nonce"], skill=live["skill"], timeout=5.0
    )
    # Consume the live nonce once so it is a protected cache entry.
    assert module._auth_error(dict(live), "req") is None
    # Stuff the cache to capacity with far-future entries.
    future = time.time() + 10_000
    module._NONCE_MARK.update(
        {f"filler{i:04x}": future for i in range(module._NONCE_MARK_MAX - 1)}
    )
    assert len(module._NONCE_MARK) == module._NONCE_MARK_MAX

    fresh = {"nonce": "ab" * 16, "skill": "1+1", "timeout": 5, "proto": 1}
    fresh["mac"] = daemon_auth.request_mac(
        token, nonce=fresh["nonce"], skill=fresh["skill"], timeout=5.0
    )
    error = module._auth_error(fresh, "req")
    assert error and "capacity" in error  # fail-closed: new request rejected
    # Live entries were not wiped: the earlier nonce still replays.
    replay = module._auth_error(dict(live), "req")
    assert replay and "replayed" in replay
    assert len(module._NONCE_MARK) == module._NONCE_MARK_MAX  # nothing cleared


# ---------------------------------------------------------------------------
# Remote Virtuoso PID knowledge
# ---------------------------------------------------------------------------


def test_handshake_carries_virtuoso_pid(authed_daemon) -> None:
    authed_daemon.virtuoso_pid = 424242
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port, daemon_token=TOKEN)
    assert client.remote_virtuoso_pid is None
    # The signed capability payload pre-populates the PID: no getpid()
    # round trip is needed at all.
    assert client.execute_skill("1+1", timeout=5).status is ExecutionStatus.SUCCESS
    assert client.remote_virtuoso_pid == 424242
    assert client.get_virtuoso_pid(timeout=5) == 424242
    assert not [r for r in authed_daemon.requests if r.get("skill", "").strip() == "getpid()"]


def test_client_pid_query_unauthenticated_is_rejected(authed_daemon) -> None:
    # A client without the token gets nothing from the daemon at all.
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port)
    assert client.get_virtuoso_pid(timeout=5) is None
    assert authed_daemon.executed == []


# ---------------------------------------------------------------------------
# SSH-side provisioning logic
# ---------------------------------------------------------------------------


class _FakeRunner:
    def __init__(
        self,
        remote_token: str | None = None,
        home: str = "/home/user1",
        race_winner_token: str | None = None,
    ):
        self.remote_token = remote_token
        self.home = home
        self.race_winner_token = race_winner_token
        self.uploads: list[tuple[str, str]] = []
        self.commands: list[str] = []

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        if command.startswith("cat "):
            if self.remote_token:
                return CommandResult(0, self.remote_token + "\n", "")
            return CommandResult(1, "", "no such file")
        if command.startswith("printf"):
            return CommandResult(0, self.home, "")
        if "(ln " in command and "bridge_token" in command:
            if self.remote_token is None:
                uploaded = self.uploads[-1][0].strip()
                self.remote_token = self.race_winner_token or uploaded
            return CommandResult(0, self.remote_token + "\n", "")
        return CommandResult(0, "", "")

    def upload_text(self, text: str, remote_path: str, timeout=None) -> CommandResult:
        self.uploads.append((text, remote_path))
        return CommandResult(0, "", "")


def test_sshclient_ensure_daemon_token_reads_existing() -> None:
    from virtuoso_bridge.transport.tunnel import SSHClient

    client = SSHClient(remote_host="server2", remote_user="user1", port=65061)
    runner = _FakeRunner(remote_token=TOKEN.upper())  # case-insensitive match
    client._ssh_runner = runner
    token = client.ensure_daemon_token()
    assert token == TOKEN
    assert client.daemon_token == TOKEN
    assert client.ensure_daemon_token() == TOKEN  # cached, no extra reads
    assert sum(1 for cmd in runner.commands if cmd.startswith("cat ")) == 1


def test_sshclient_ensure_daemon_token_provisions_atomically() -> None:
    from virtuoso_bridge.transport.tunnel import SSHClient

    client = SSHClient(remote_host="server2", remote_user="user1", port=65061)
    runner = _FakeRunner(remote_token=None)
    client._ssh_runner = runner

    token = client.ensure_daemon_token()

    assert daemon_auth.is_valid_token(token)
    # Token goes to a unique temp path, then is linked into place only if
    # absent and read back so concurrent starters converge on one value.
    uploaded_path = runner.uploads[0][1]
    assert uploaded_path.startswith("/home/user1/.virtuoso-bridge/bridge_token.tmp.")
    assert any(
        "chmod 600" in cmd and "(ln " in cmd and "cat " in cmd
        for cmd in runner.commands
    )
    assert not any("mv -f" in cmd for cmd in runner.commands)
    assert any("chmod 700" in cmd for cmd in runner.commands)


def test_sshclient_ensure_daemon_token_adopts_remote_race_winner() -> None:
    from virtuoso_bridge.transport.tunnel import SSHClient

    client = SSHClient(remote_host="server2", remote_user="user1", port=65061)
    runner = _FakeRunner(remote_token=None, race_winner_token=TOKEN)
    client._ssh_runner = runner

    assert client.ensure_daemon_token() == TOKEN
    assert client.daemon_token == TOKEN
    assert runner.uploads[0][0].strip() != TOKEN


def test_sshclient_ensure_daemon_token_fatal_without_optin(monkeypatch) -> None:
    from virtuoso_bridge.transport.tunnel import SSHClient

    monkeypatch.delenv(daemon_auth.UNAUTH_OPTIN_ENV, raising=False)
    client = SSHClient(remote_host="server2", remote_user="user1", port=65061)
    client._ssh_runner = _FakeRunner(remote_token=None, home="")  # $HOME unresolvable
    with pytest.raises(daemon_auth.DaemonTokenError, match="explicitly"):
        client.ensure_daemon_token()


def test_sshclient_ensure_daemon_token_optin_degrades(monkeypatch) -> None:
    from virtuoso_bridge.transport.tunnel import SSHClient

    monkeypatch.setenv(daemon_auth.UNAUTH_OPTIN_ENV, "1")
    client = SSHClient(remote_host="server2", remote_user="user1", port=65061)
    client._ssh_runner = _FakeRunner(remote_token=None, home="")
    assert client.ensure_daemon_token() is None


def test_sshclient_ensure_daemon_token_local_mode_uses_filesystem(token_home) -> None:
    from virtuoso_bridge.transport.tunnel import SSHClient

    client = SSHClient(remote_host="localhost", port=65432)
    token = client.ensure_daemon_token()
    assert daemon_auth.is_valid_token(token)
    assert token == daemon_auth.read_local_token()


@pytest.mark.parametrize("pause", [None, BlockingIOError(errno.EAGAIN, "not ready")])
@pytest.mark.parametrize("prefix", [b"", b"\x02par"])
def test_real_daemon_waits_for_nonblocking_response(tmp_path, monkeypatch, pause, prefix):
    module = _import_py3_daemon(monkeypatch, tmp_path)
    remaining = b"tial\x1e" if prefix else b"\x02partial\x1e"
    chunks = iter([*(bytes([c]) for c in prefix), pause,
                   *(bytes([c]) for c in remaining)])

    def read(_):
        item = next(chunks)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sys.stdin.buffer, "read", read)
    assert module.read_until_delimiter() == b"\x02partial"


@pytest.mark.parametrize("prefix", [b"", b"\x02partial"])
def test_real_daemon_reports_eof_without_timeout(tmp_path, monkeypatch, prefix):
    module = _import_py3_daemon(monkeypatch, tmp_path)
    chunks = iter([*(bytes([c]) for c in prefix), b""])
    monkeypatch.setattr(sys.stdin.buffer, "read", lambda _: next(chunks))
    response = module.read_until_delimiter()
    assert response.startswith(b"\x15")
    assert b"EOF" in response
    assert b"TimeoutError" not in response


@pytest.mark.parametrize("prefix", [b"", b"\x02partial"])
@pytest.mark.parametrize("pause", [None, BlockingIOError(errno.EAGAIN, "not ready")])
def test_real_daemon_nonblocking_wait_obeys_timeout(tmp_path, monkeypatch, prefix, pause):
    module = _import_py3_daemon(monkeypatch, tmp_path)
    chunks = iter(bytes([c]) for c in prefix)

    def read(_):
        try:
            return next(chunks)
        except StopIteration:
            module.timeout_flag = True
            if isinstance(pause, Exception):
                raise pause
            return pause

    monkeypatch.setattr(sys.stdin.buffer, "read", read)
    assert module.read_until_delimiter() == b"\x15TimeoutError"

"""Tests for SSH-bootstrapped bridge token authentication (daemon_auth).

Covers the HMAC wire protocol in both directions, the client enforcement
paths, and — explicitly — that non-SSH access methods (VirtuosoClient.local,
tools/skill_exec.py) keep working unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
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
# Wire-level fake daemon with (optional) token auth
# ---------------------------------------------------------------------------


class _AuthDaemon:
    """RAMIC-wire daemon implementing the token auth contract."""

    def __init__(self, token: str | None = TOKEN):
        self.token = token
        self.requests: list[dict] = []  # received (pre-auth)
        self.executed: list[str] = []  # post-auth SKILL executions
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

            def nak(message: str) -> None:
                conn.sendall((NAK + message).encode("utf-8"))

            if self.token:
                nonce, mac = req.get("nonce"), req.get("mac")
                if not nonce or not mac:
                    nak("AuthError: bridge token required")
                    return
                expected = hmac.new(
                    self.token.encode(), str(nonce).encode(), hashlib.sha256
                ).hexdigest()
                if expected != str(mac).lower():
                    nak("AuthError: bridge token mismatch")
                    return

            skill = req["skill"]
            self.executed.append(skill)
            body = '"2"' if skill.strip() == "1+1" else "nil"
            if self.token and req.get("nonce"):
                resp_mac = hmac.new(
                    self.token.encode(),
                    (str(req["nonce"]) + ":resp").encode(),
                    hashlib.sha256,
                ).hexdigest()
                conn.sendall((STX + resp_mac + body).encode("utf-8"))
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
    daemon = _AuthDaemon(token=None)  # pre-token-auth or auth-disabled daemon
    yield daemon
    daemon.close()


@pytest.fixture()
def token_home(monkeypatch, tmp_path):
    """Isolated HOME so tests never touch the real ~/.virtuoso-bridge."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# daemon_auth helpers
# ---------------------------------------------------------------------------


def test_sign_and_verify_roundtrip() -> None:
    nonce = "12" * 16
    raw = STX + daemon_auth.response_mac(TOKEN, nonce) + "hello"
    assert daemon_auth.verify_response(raw, TOKEN, nonce) == STX + "hello"


def test_verify_rejects_wrong_token() -> None:
    nonce = "12" * 16
    raw = STX + daemon_auth.response_mac(TOKEN, nonce) + "hello"
    with pytest.raises(daemon_auth.DaemonAuthError, match="spoofed listener"):
        daemon_auth.verify_response(raw, WRONG_TOKEN, nonce)


def test_verify_rejects_missing_mac() -> None:
    with pytest.raises(daemon_auth.DaemonAuthError, match="restart"):
        daemon_auth.verify_response(NAK + "TimeoutError", TOKEN, "12" * 16)


def test_verify_rejects_non_hex_prefix() -> None:
    with pytest.raises(daemon_auth.DaemonAuthError, match="restart"):
        daemon_auth.verify_response(STX + "zz" * 32 + "body", TOKEN, "12" * 16)


def test_read_or_create_local_token_is_idempotent(token_home) -> None:
    first = daemon_auth.read_or_create_local_token()
    assert daemon_auth.is_valid_token(first)
    second = daemon_auth.read_or_create_local_token()
    assert first == second
    assert daemon_auth.token_path().exists()


def test_read_local_token_never_creates(token_home) -> None:
    assert daemon_auth.read_local_token() is None
    assert not daemon_auth.token_path().exists()


# ---------------------------------------------------------------------------
# Client <-> daemon over the wire
# ---------------------------------------------------------------------------


def test_client_with_token_executes(authed_daemon) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port, daemon_token=TOKEN)
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.SUCCESS
    assert result.output == '"2"'
    assert authed_daemon.requests[0]["mac"] == daemon_auth.sign_request(TOKEN, authed_daemon.requests[0]["nonce"])


def test_client_with_wrong_token_is_rejected(authed_daemon) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port, daemon_token=WRONG_TOKEN)
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.ERROR
    assert "Bridge authentication failed" in result.errors[0]
    assert "token mismatch" in result.errors[0]
    # The foreign daemon must not have executed anything.
    assert authed_daemon.executed == []


def test_client_refuses_squatter_without_token(legacy_daemon) -> None:
    # Daemon predates token auth (or runs auth-disabled): client must not
    # trust its responses.
    client = VirtuosoClient(host="127.0.0.1", port=legacy_daemon.port, daemon_token=TOKEN)
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.ERROR
    assert "did not authenticate its response" in result.errors[0]
    assert len(legacy_daemon.requests) == 1  # request reached it...


def test_legacy_client_is_rejected_by_authed_daemon(authed_daemon) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=authed_daemon.port)  # no token
    result = client.execute_skill("1+1", timeout=5)
    assert result.status is ExecutionStatus.ERROR
    assert "bridge token required" in result.errors[0]
    assert authed_daemon.executed == []


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


def test_skill_exec_tool_signs_and_verifies(authed_daemon, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    token_file = tmp_path / "bridge_token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")

    result, error = tool.execute(
        "1+1", host="127.0.0.1", port=authed_daemon.port, timeout=5,
        token=tool._load_token(str(token_file)),
    )
    assert error is None and result == '"2"'


def test_skill_exec_tool_rejected_without_token(authed_daemon, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_exec", Path(__file__).resolve().parents[1] / "tools" / "skill_exec.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    result, error = tool.execute(
        "1+1", host="127.0.0.1", port=authed_daemon.port, timeout=5, token=None
    )
    assert result is None and "AuthError" in error


# ---------------------------------------------------------------------------
# Real daemon script (POSIX only: daemon needs fcntl)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.mark.skipif(os.name == "nt", reason="bridge daemon requires fcntl (POSIX)")
def test_real_daemon_rejects_unauthenticated_requests(tmp_path) -> None:
    daemon_src = (
        Path(__file__).resolve().parents[1]
        / "src" / "virtuoso_bridge" / "virtuoso" / "basic" / "resources"
        / "ramic_bridge_daemon_3.py"
    )
    token_path = tmp_path / "bridge_token"
    port = _free_port()
    env = dict(os.environ, RB_TOKEN_PATH=str(token_path))
    proc = subprocess.Popen(
        [sys.executable, str(daemon_src), "127.0.0.1", str(port)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
    )
    try:
        # Wait for the listener; then confirm the token file was provisioned.
        for _ in range(100):
            try:
                probe = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                probe.close()
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail("daemon exited early")
                import time

                time.sleep(0.05)
        assert token_path.exists(), "daemon must auto-provision its token file"

        def raw_request(payload: dict) -> str:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
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

        reply = raw_request({"skill": "1+1", "timeout": 1})
        assert reply.startswith(NAK) and "bridge token required" in reply

        nonce = "34" * 16
        reply = raw_request(
            {"skill": "1+1", "timeout": 1, "nonce": nonce,
             "mac": daemon_auth.sign_request(WRONG_TOKEN, nonce)}
        )
        assert reply.startswith(NAK) and "token mismatch" in reply
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_daemon_scripts_compile() -> None:
    resources = (
        Path(__file__).resolve().parents[1]
        / "src" / "virtuoso_bridge" / "virtuoso" / "basic" / "resources"
    )
    for name in ("ramic_bridge_daemon_3.py", "ramic_bridge_daemon_27.py"):
        rc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(resources / name)],
            capture_output=True,
        )
        assert rc.returncode == 0, rc.stderr.decode()


# ---------------------------------------------------------------------------
# SSH-side provisioning logic
# ---------------------------------------------------------------------------


class _FakeRunner:
    def __init__(self, remote_token: str | None = None, home: str = "/home/user1"):
        self.remote_token = remote_token
        self.home = home
        self.uploads: dict[str, str] = []
        self.commands: list[str] = []

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        if command.startswith("cat "):
            if self.remote_token:
                return CommandResult(0, self.remote_token + "\n", "")
            return CommandResult(1, "", "no such file")
        if command.startswith("printf"):
            return CommandResult(0, self.home, "")
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


def test_sshclient_ensure_daemon_token_provisions_when_missing() -> None:
    from virtuoso_bridge.transport.tunnel import SSHClient

    client = SSHClient(remote_host="server2", remote_user="user1", port=65061)
    runner = _FakeRunner(remote_token=None)
    client._ssh_runner = runner

    token = client.ensure_daemon_token()

    assert daemon_auth.is_valid_token(token)
    assert runner.uploads and runner.uploads[0][1] == "/home/user1/.virtuoso-bridge/bridge_token"
    assert any("chmod 600" in cmd for cmd in runner.commands)
    assert any("chmod 700" in cmd for cmd in runner.commands)


def test_sshclient_ensure_daemon_token_local_mode_uses_filesystem(token_home) -> None:
    from virtuoso_bridge.transport.tunnel import SSHClient

    client = SSHClient(remote_host="localhost", port=65432)
    token = client.ensure_daemon_token()
    assert daemon_auth.is_valid_token(token)
    assert token == daemon_auth.read_local_token()

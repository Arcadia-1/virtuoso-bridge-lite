"""Tests for the cross-user daemon identity guard (daemon_guard)."""

from __future__ import annotations

import json
import socket
import threading

import pytest

from virtuoso_bridge.daemon_guard import (
    OVERRIDE_ENV,
    check_daemon_user,
    classify_daemon_query_failure,
    expected_remote_user,
    query_daemon_user,
)
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.basic.bridge import (
    VirtuosoClient,
    _default_remote_port,
)

STX, NAK, RS = "\x02", "\x15", "\x1e"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeDaemon:
    """Minimal RAMIC-wire daemon: answers identity queries as *user*."""

    def __init__(self, user: str = "user2", virtuoso_pid: str = "424242"):
        self.user = user
        self.virtuoso_pid = virtuoso_pid
        self.executed: list[str] = []
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
            # Capability handshake (no skill field, nothing executes):
            # these doubles model an explicitly auth-disabled daemon.
            if req.get("op") == "hello":
                pid = int(self.virtuoso_pid) if str(self.virtuoso_pid).isdigit() else None
                caps = json.dumps(
                    {"proto": 1, "auth": "off", "daemon": "fake-guard",
                     "virtuoso_pid": pid}
                )
                conn.sendall(f"{STX}{caps}".encode("utf-8"))
                return
            skill = req["skill"]
            self.executed.append(skill)
            if 'getShellEnvVar("USER")' in skill or 'getShellEnvVar("LOGNAME")' in skill:
                body = f'"{self.user}"' if self.user else "nil"
            elif skill.strip() == "getpid()":
                body = self.virtuoso_pid
            else:
                body = "nil"
            conn.sendall(f"{STX}{body}{RS}".encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            try:
                conn.sendall(f"{NAK}{exc}{RS}".encode("utf-8"))
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


class FakeRunner:
    """SSH runner for the tunnel login *whoami*; stat/ps report *owner*."""

    def __init__(self, whoami: str = "user1", owner: str = "user2"):
        self.whoami = whoami
        self.owner = owner
        self.commands: list[str] = []

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        cmd = command.strip()
        if cmd == "whoami":
            return CommandResult(returncode=0, stdout=f"{self.whoami}\n", stderr="")
        if cmd.startswith("stat -c %U /proc/") or cmd.startswith("ps -o user= -p"):
            if self.owner:
                return CommandResult(returncode=0, stdout=f"{self.owner}\n", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="no such process")
        return CommandResult(returncode=0, stdout="FREE\n", stderr="")


class FakeClient:
    """Canned execute_skill answers; mirrors the VirtuosoClient surface."""

    def __init__(self, answers: dict[str, str] | None = None, error: str | None = None):
        self.answers = answers or {}
        self.error = error
        self.ssh_runner = None
        self.executed: list[str] = []

    def execute_skill(self, skill: str, timeout=None) -> VirtuosoResult:
        self.executed.append(skill)
        if self.error is not None:
            return VirtuosoResult(status=ExecutionStatus.ERROR, errors=[self.error])
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=self.answers.get(skill, "nil"))


@pytest.fixture()
def foreign_daemon():
    daemon = FakeDaemon(user="user2")
    yield daemon
    daemon.close()


# ---------------------------------------------------------------------------
# Port hash
# ---------------------------------------------------------------------------


def test_default_remote_port_stable_and_bounded(monkeypatch) -> None:
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    assert _default_remote_port("designer") == _default_remote_port("designer")
    for user in ("a", "designer", "user1", "x" * 64):
        assert 65000 <= _default_remote_port(user) <= 65499
    # No explicit user and nothing configured -> documented default.
    assert _default_remote_port(None) == 65432
    assert _default_remote_port("") == 65432


def test_default_remote_port_separates_anagrams(monkeypatch) -> None:
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    # sum(ord)%500 (the old scheme) gave identical ports to any permutation.
    assert _default_remote_port("user1") != _default_remote_port("er1us")
    assert _default_remote_port("liuyan") != _default_remote_port("yulian")


# ---------------------------------------------------------------------------
# Expected user resolution
# ---------------------------------------------------------------------------


def test_expected_user_prefers_configured_env(monkeypatch) -> None:
    monkeypatch.setenv("VB_REMOTE_USER", "designer")
    monkeypatch.setenv("VB_REMOTE_USER_gpu1", "other")
    assert expected_remote_user(None) == "designer"
    assert expected_remote_user("gpu1") == "other"


def test_expected_user_falls_back_to_ssh_whoami(monkeypatch) -> None:
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    runner = FakeRunner(whoami="user1")
    assert expected_remote_user(None, runner=runner) == "user1"


def test_expected_user_empty_without_env_or_runner(monkeypatch) -> None:
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    assert expected_remote_user(None) == ""


# ---------------------------------------------------------------------------
# Daemon-side user query layering
# ---------------------------------------------------------------------------


def test_query_uses_user_env_var() -> None:
    client = FakeClient({'getShellEnvVar("USER")': "user2"})
    assert query_daemon_user(client) == "user2"
    assert client.executed == ['getShellEnvVar("USER")']


def test_query_falls_back_to_logname() -> None:
    client = FakeClient({'getShellEnvVar("LOGNAME")': "user2"})
    assert query_daemon_user(client) == "user2"
    assert 'getShellEnvVar("LOGNAME")' in client.executed


def test_query_falls_back_to_process_owner_over_ssh() -> None:
    client = FakeClient({"getpid()": "424242"})
    runner = FakeRunner(owner="user2")
    assert query_daemon_user(client, runner=runner) == "user2"
    assert any(cmd.startswith("stat -c %U /proc/424242") for cmd in runner.commands)
    # The discovered PID is retained on the client for later cross-checks.
    assert client._remote_virtuoso_pid == 424242


def test_query_empty_when_every_layer_fails() -> None:
    client = FakeClient()
    runner = FakeRunner(owner="")
    assert query_daemon_user(client, runner=runner) == ""


def test_query_raises_when_daemon_silent() -> None:
    client = FakeClient(error="Empty response from daemon")
    with pytest.raises(RuntimeError, match="Empty response"):
        query_daemon_user(client)


# ---------------------------------------------------------------------------
# check_daemon_user policy
# ---------------------------------------------------------------------------


def test_check_rejects_user_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("VB_REMOTE_USER", "user1")
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    check = check_daemon_user(FakeClient({'getShellEnvVar("USER")': "user2"}), profile=None)
    assert not check.ok
    assert check.expected_user == "user1"
    assert check.daemon_user == "user2"
    assert "user2" in check.error and "user1" in check.error


def test_check_skips_when_daemon_not_loaded(monkeypatch) -> None:
    monkeypatch.setenv("VB_REMOTE_USER", "user1")
    check = check_daemon_user(FakeClient(error="Empty response from daemon"), profile=None)
    assert check.ok
    assert check.skipped


def test_check_rejects_when_something_answers_but_hides_identity(monkeypatch) -> None:
    # Hole B: Virtuoso launched without $USER/$LOGNAME and no SSH runner to
    # resolve the process owner — must fail closed, never silently pass.
    monkeypatch.setenv("VB_REMOTE_USER", "user1")
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    check = check_daemon_user(FakeClient(), profile=None)
    assert not check.ok
    assert "did not report its Unix user" in check.error


def test_check_rejects_indeterminate_timeout(monkeypatch) -> None:
    # Hole D: a busy foreign daemon answers TCP but not the identity query.
    monkeypatch.setenv("VB_REMOTE_USER", "user1")
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    check = check_daemon_user(FakeClient(error="Socket timeout after 5s"), profile=None)
    assert not check.ok
    assert "could not determine" in check.error


def test_check_override_env_allows_any_daemon(monkeypatch) -> None:
    monkeypatch.setenv("VB_REMOTE_USER", "user1")
    monkeypatch.setenv(OVERRIDE_ENV, "1")
    check = check_daemon_user(FakeClient({'getShellEnvVar("USER")': "user2"}), profile=None)
    assert check.ok
    assert check.daemon_user == "user2"


def test_check_permissive_when_expected_unknown(monkeypatch) -> None:
    # Local mode: no VB_REMOTE_USER, no SSH runner -> nothing to compare.
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    client = FakeClient({'getShellEnvVar("USER")': "someone"})
    check = check_daemon_user(client, profile=None)
    assert check.ok
    assert check.daemon_user == "someone"


def test_check_resolves_expected_user_via_whoami(monkeypatch) -> None:
    # Hole A: ssh-config-only setup without VB_REMOTE_USER must still be
    # checked, against the SSH login name.
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    client = FakeClient({'getShellEnvVar("USER")': "user2"})
    client.ssh_runner = FakeRunner(whoami="user1", owner="user2")
    check = check_daemon_user(client, profile=None)
    assert not check.ok
    assert check.expected_user == "user1"


# ---------------------------------------------------------------------------
# Client construction paths
# ---------------------------------------------------------------------------


class _FakeTunnel:
    def __init__(self, port: int, ssh_runner):
        self.port = port
        self.remote_host = "server2"
        self._remote_host = "server2"
        self._remote_user = "user1"
        self._profile = None
        self._ssh_runner = ssh_runner

    @property
    def ssh_runner(self):
        return self._ssh_runner

    def warm(self, timeout=15):
        pass

    is_tunnel_alive = True


def test_from_tunnel_refuses_foreign_daemon(monkeypatch, foreign_daemon) -> None:
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    with pytest.raises(RuntimeError, match="identity check failed"):
        VirtuosoClient.from_tunnel(_FakeTunnel(foreign_daemon.port, FakeRunner()))


def test_from_tunnel_executes_only_after_identity_match(monkeypatch, foreign_daemon) -> None:
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    # The fake daemon advertises auth=off: an explicit client opt-in is
    # required for a token-less client to talk to it at all.
    monkeypatch.setenv("VB_ALLOW_UNAUTHENTICATED_DAEMON", "1")
    # Same user on both sides: guard passes, execution reaches the daemon.
    runner = FakeRunner(whoami="user2", owner="user2")
    client = VirtuosoClient.from_tunnel(_FakeTunnel(foreign_daemon.port, runner))
    result = client.execute_skill("1+1", timeout=5)
    assert result.status == ExecutionStatus.SUCCESS
    assert any("getShellEnvVar" in s for s in foreign_daemon.executed)


def test_from_tunnel_skips_guard_for_local_tunnels(monkeypatch, foreign_daemon) -> None:
    # No SSH runner -> local mode semantics: no cross-user exposure check.
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    monkeypatch.setenv("VB_ALLOW_UNAUTHENTICATED_DAEMON", "1")
    client = VirtuosoClient.from_tunnel(_FakeTunnel(foreign_daemon.port, None))
    result = client.execute_skill("1+1", timeout=5)
    assert result.status == ExecutionStatus.SUCCESS


def test_client_rejects_cross_user_before_any_skill_execution(
    monkeypatch, foreign_daemon
) -> None:
    # End to end: the mismatch must be caught before user SKILL runs in the
    # foreign session.
    monkeypatch.delenv("VB_REMOTE_USER", raising=False)
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    client = VirtuosoClient(host="127.0.0.1", port=foreign_daemon.port, timeout=5)
    client._tunnel = _FakeTunnel(foreign_daemon.port, FakeRunner())
    foreign_daemon.executed.clear()
    with pytest.raises(RuntimeError, match="identity check failed"):
        client._reject_cross_user_daemon_if_reachable(profile=None, timeout=5)
    # Only identity probes may reach the foreign session — no user SKILL.
    assert all("getShellEnvVar" in s or s.strip() == "getpid()" for s in foreign_daemon.executed)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("Connection refused to 127.0.0.1:65061", "not_loaded"),
        ("Empty response from daemon", "not_loaded"),
        ("Socket error: [WinError 10054] reset", "not_loaded"),
        ("Socket error: [WinError 10061] refused", "not_loaded"),
        ("Socket timeout after 5s", "indeterminate"),
        ("Socket error: boom", "indeterminate"),
    ],
)
def test_classify_daemon_query_failure(error: str, expected: str) -> None:
    assert classify_daemon_query_failure(error) == expected

"""Local cache of a remote daemon's token and verified identity.

It saves every new client an SSH round trip for the token and one for the
daemon owner, without trusting anything the signed handshake cannot confirm.
"""

from __future__ import annotations

import os
import stat
import types

import pytest

from virtuoso_bridge import daemon_auth
from virtuoso_bridge.daemon_guard import DaemonUserCheck
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
POSIX = os.name != "nt"


class _TokenRunner:
    def __init__(self, token: str):
        self.token = token
        self.commands: list[str] = []

    def run_command(self, command: str, timeout=None) -> CommandResult:
        self.commands.append(command)
        return CommandResult(returncode=0, stdout=self.token + "\n", stderr="")


def _ssh(token: str = TOKEN_A) -> SSHClient:
    ssh = SSHClient(remote_host="eda-host", remote_user="designer", port=65000)
    ssh._ssh_runner = _TokenRunner(token)
    return ssh


# ---------------------------------------------------------------- cache file


def test_cache_file_is_private_and_merges_fields(tmp_path):
    path = tmp_path / "dir" / "designer@eda-host.json"
    daemon_auth.update_remote_cache(path, token=TOKEN_A)
    daemon_auth.update_remote_cache(path, verified={"daemon_instance": "x"})
    assert daemon_auth.read_remote_cache(path) == {"token": TOKEN_A, "verified": {"daemon_instance": "x"}}
    daemon_auth.update_remote_cache(path, verified=None)
    assert daemon_auth.read_remote_cache(path) == {"token": TOKEN_A}
    if POSIX:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_a_cache_others_can_read_is_ignored(tmp_path):
    path = tmp_path / "c.json"
    daemon_auth.update_remote_cache(path, token=TOKEN_A)
    os.chmod(path, 0o644)
    assert daemon_auth.read_remote_cache(path) == {}


def test_a_malformed_cache_is_ignored(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("not json")
    if POSIX:
        os.chmod(path, 0o600)
    assert daemon_auth.read_remote_cache(path) == {}


# ---------------------------------------------------------------- token


def test_token_is_fetched_once_then_read_from_the_cache():
    first = _ssh()
    assert first.ensure_daemon_token() == TOKEN_A
    assert len(first._ssh_runner.commands) == 1 and not first.daemon_token_from_cache

    second = _ssh(token=TOKEN_B)
    assert second.ensure_daemon_token() == TOKEN_A
    assert second._ssh_runner.commands == [] and second.daemon_token_from_cache


def test_refresh_drops_the_cache_and_fetches_over_ssh():
    _ssh().ensure_daemon_token()
    ssh = _ssh(token=TOKEN_B)
    assert ssh.ensure_daemon_token() == TOKEN_A
    daemon_auth.update_remote_cache(ssh.remote_cache_path(), verified={"daemon_instance": "x"})

    assert ssh.refresh_daemon_token() == TOKEN_B
    assert daemon_auth.read_remote_cache(ssh.remote_cache_path()) == {"token": TOKEN_B}


# ---------------------------------------------------------------- client


def _client(ssh, outcomes):
    client = VirtuosoClient(host="127.0.0.1", port=9, timeout=5, tunnel=ssh, daemon_token=ssh.ensure_daemon_token())
    calls = []

    def handshake(self, deadline, refresh=False):
        calls.append(self.daemon_token)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self._daemon_caps = outcome
        return outcome

    client._ensure_daemon_capabilities = types.MethodType(handshake, client)
    return client, calls


CAPS = {"auth": "on", "daemon_instance": "i" * 32, "virtuoso_pid": 4242}


def test_a_rejected_cached_token_is_replaced_by_one_from_ssh():
    _ssh().ensure_daemon_token()
    ssh = _ssh(token=TOKEN_B)
    client, calls = _client(ssh, [daemon_auth.DaemonAuthError("AuthError"), dict(CAPS)])

    assert client._signed_handshake(5) == CAPS
    assert calls == [TOKEN_A, TOKEN_B] and client.daemon_token == TOKEN_B


def test_an_unchecked_cached_token_is_replaced_when_the_daemon_does_not_answer():
    _ssh().ensure_daemon_token()
    ssh = _ssh(token=TOKEN_B)
    client, _ = _client(ssh, [ConnectionRefusedError()])

    assert client._signed_handshake(5) is None
    assert client.daemon_token == TOKEN_B


def _identity(monkeypatch, client, checks):
    calls = []

    def check(_client, profile=None, timeout=5, runner=None):
        calls.append(1)
        return checks.pop(0)

    monkeypatch.setattr("virtuoso_bridge.daemon_guard.check_daemon_user", check)
    return calls


def test_a_verified_daemon_instance_skips_the_owner_lookup(monkeypatch):
    ssh = _ssh()
    client, _ = _client(ssh, [dict(CAPS)])
    calls = _identity(monkeypatch, client, [DaemonUserCheck(ok=True, expected_user="designer", daemon_user="designer")])
    client._reject_cross_user_daemon_if_reachable(profile=None)
    assert calls == [1]

    again, _ = _client(_ssh(), [dict(CAPS)])
    calls = _identity(monkeypatch, again, [])
    again._reject_cross_user_daemon_if_reachable(profile=None)
    assert calls == []


@pytest.mark.parametrize("caps", [
    # A restarted daemon has a new instance id.
    dict(CAPS, daemon_instance="j" * 32),
    # An unsigned handshake proves nothing.
    dict(CAPS, auth="off"),
])
def test_other_daemons_are_checked_again(monkeypatch, caps):
    first, _ = _client(_ssh(), [dict(CAPS)])
    _identity(monkeypatch, first, [DaemonUserCheck(ok=True, expected_user="designer", daemon_user="designer")])
    first._reject_cross_user_daemon_if_reachable(profile=None)

    client, _ = _client(_ssh(), [caps])
    calls = _identity(monkeypatch, client, [DaemonUserCheck(ok=True, expected_user="designer", daemon_user="designer")])
    client._reject_cross_user_daemon_if_reachable(profile=None)
    assert calls == [1]


@pytest.mark.parametrize("check", [
    DaemonUserCheck(ok=True, expected_user="designer", skipped=True),
    # The cross-user override passes a foreign daemon; it is never remembered.
    DaemonUserCheck(ok=True, expected_user="designer", daemon_user="someone-else"),
])
def test_only_a_same_user_verification_is_remembered(monkeypatch, check):
    client, _ = _client(_ssh(), [dict(CAPS)])
    _identity(monkeypatch, client, [check])
    client._reject_cross_user_daemon_if_reachable(profile=None)
    assert "verified" not in daemon_auth.read_remote_cache(client._tunnel.remote_cache_path())

"""Small daemon identity guard for cross-user Virtuoso sessions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from virtuoso_bridge.models import ExecutionStatus


_TRUTHY = {"1", "true", "yes", "on"}
OVERRIDE_ENV = "VB_ALLOW_CROSS_USER_DAEMON"


@dataclass(frozen=True)
class DaemonUserCheck:
    ok: bool
    expected_user: str = ""
    daemon_user: str = ""
    error: str = ""


@dataclass(frozen=True)
class DaemonHostCheck:
    ok: bool
    configured_host: str = ""
    endpoint_hostname: str = ""
    daemon_hostname: str = ""
    error: str = ""


def cross_user_override_enabled() -> bool:
    return os.getenv(OVERRIDE_ENV, "").strip().lower() in _TRUTHY


def expected_remote_user(profile: str | None) -> str:
    suffix = f"_{profile}" if profile else ""
    return os.getenv(f"VB_REMOTE_USER{suffix}", "").strip()


def clean_skill_output(value: str | None) -> str:
    text = (value or "").strip()
    if text.lower() == "nil":
        return ""
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text.replace("\\n", "\n").replace('\\"', '"')


def _normalized_hostnames(host: str) -> set[str]:
    value = (host or "").strip().rstrip(".").lower()
    if not value:
        return set()
    return {value, value.split(".", 1)[0]}


def check_daemon_host(
    *,
    daemon_hostname: str,
    endpoint_hostname: str,
    configured_host: str,
) -> DaemonHostCheck:
    """Compare the banner host with the OS host reached by the tunnel endpoint."""
    daemon_names = _normalized_hostnames(daemon_hostname)
    # SSH aliases are not necessarily related to the target's OS hostname.
    # Only diagnose a mismatch after probing the endpoint hostname itself.
    endpoint_names = _normalized_hostnames(endpoint_hostname)
    ok = not daemon_names or not endpoint_names or bool(daemon_names & endpoint_names)
    error = ""
    if not ok:
        error = (
            f"daemon banner reports host {daemon_hostname!r}, but the configured "
            f"tunnel endpoint {configured_host!r} resolves over SSH to "
            f"{endpoint_hostname or '(unknown)'!r}"
        )
    return DaemonHostCheck(
        ok=ok,
        configured_host=configured_host,
        endpoint_hostname=endpoint_hostname,
        daemon_hostname=daemon_hostname,
        error=error,
    )


def query_daemon_user(client: Any, *, timeout: int = 5) -> str:
    result = client.execute_skill('getShellEnvVar("USER")', timeout=timeout)
    if result.status != ExecutionStatus.SUCCESS:
        raise RuntimeError("; ".join(result.errors) or "daemon USER query failed")
    return clean_skill_output(result.output)


def check_daemon_user(client: Any, *, profile: str | None, timeout: int = 5) -> DaemonUserCheck:
    expected = expected_remote_user(profile)
    if not expected:
        return DaemonUserCheck(ok=True, expected_user=expected)

    daemon_user = query_daemon_user(client, timeout=timeout)
    if cross_user_override_enabled():
        return DaemonUserCheck(ok=True, expected_user=expected, daemon_user=daemon_user)
    if daemon_user and daemon_user != expected:
        return DaemonUserCheck(
            ok=False,
            expected_user=expected,
            daemon_user=daemon_user,
            error=(
                f"daemon Unix user {daemon_user!r} does not match configured "
                f"VB_REMOTE_USER {expected!r}"
            ),
        )
    return DaemonUserCheck(ok=True, expected_user=expected, daemon_user=daemon_user)

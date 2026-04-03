"""Remote scratch paths and username resolution for SSH uploads."""

from __future__ import annotations

import getpass
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from virtuoso_bridge.transport.ssh import SSHRunner

REMOTE_SCRATCH_ROOT_ENV = "VB_REMOTE_SCRATCH_ROOT"

def remote_scratch_root() -> str:
    """Base directory for remote scratch (default ``/tmp``)."""
    return os.environ.get(REMOTE_SCRATCH_ROOT_ENV, "/tmp").rstrip("/")

def sanitize_username_for_path(username: str) -> str:
    """Make a username safe as a single path segment."""
    s = username.strip()
    if not s:
        return "unknown"
    if re.match(r"^[a-zA-Z0-9._-]+$", s):
        return s[:64]
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)[:64]

def resolve_remote_username(
    *,
    configured_user: str | None,
    runner: SSHRunner | None = None,
    fallback: str = "unknown",
) -> str:
    """Resolve SSH username: configured_user > whoami > getpass > fallback."""
    u = (configured_user or "").strip()
    if u:
        return sanitize_username_for_path(u)
    if runner is not None:
        whoami_result = runner.run_command("whoami")
        if whoami_result.returncode == 0 and whoami_result.stdout.strip():
            return sanitize_username_for_path(whoami_result.stdout.strip())
        return fallback
    try:
        local = getpass.getuser()
        if local:
            return sanitize_username_for_path(local)
    except Exception:
        pass
    for key in ("USER", "USERNAME"):
        v = os.environ.get(key, "").strip()
        if v:
            return sanitize_username_for_path(v)
    return fallback

def default_virtuoso_bridge_dir(username: str, leaf: str) -> str:
    """Return ``{scratch}/virtuoso_bridge_{user}/{leaf}``."""
    safe = sanitize_username_for_path(username)
    root = remote_scratch_root()
    leaf_norm = leaf.strip("/").replace("\\", "/")
    return f"{root}/virtuoso_bridge_{safe}/{leaf_norm}"

REMOTE_SPECTRE_LEAF = "virtuoso_bridge_spectre"

def default_remote_spectre_work_dir(username: str) -> str:
    """Default remote scratch for Spectre simulations."""
    return default_virtuoso_bridge_dir(username, REMOTE_SPECTRE_LEAF)

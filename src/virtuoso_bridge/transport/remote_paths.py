"""Remote scratch paths and username resolution for SSH uploads."""

from __future__ import annotations

import getpass
import os
import re
import socket
from typing import TYPE_CHECKING

from virtuoso_bridge.env import load_vb_env

if TYPE_CHECKING:
    from virtuoso_bridge.transport.ssh import SSHRunner

REMOTE_SCRATCH_ROOT_ENV = "VB_REMOTE_SCRATCH_ROOT"
CLIENT_ID_ENV = "VB_CLIENT_ID"


def remote_scratch_root() -> str:
    """Base directory for remote scratch (default ``/tmp``)."""
    load_vb_env()
    return os.environ.get(REMOTE_SCRATCH_ROOT_ENV, "/tmp").rstrip("/")


def sanitize_username_for_path(username: str) -> str:
    """Make a username safe as a single path segment."""
    s = username.strip()
    if not s:
        return "unknown"
    if re.match(r"^[a-zA-Z0-9._-]+$", s):
        return s[:64]
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)[:64]


def sanitize_client_id_for_path(client_id: str) -> str:
    """Make a local client identity safe as a single path segment."""
    s = client_id.strip()
    if not s:
        return "unknown_client"
    if re.match(r"^[a-zA-Z0-9._-]+$", s):
        return s[:64]
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)[:64]


def resolve_client_id(profile: str | None = None) -> str:
    """Resolve local client identity for remote scratch isolation.

    This is the machine/account running the bridge locally, not the remote
    EDA host.  Override with ``VB_CLIENT_ID`` or ``VB_CLIENT_ID_<profile>``.
    """
    load_vb_env()
    suffix = f"_{profile}" if profile else ""
    for key in (f"{CLIENT_ID_ENV}{suffix}", CLIENT_ID_ENV, "USERNAME", "USER"):
        v = os.environ.get(key, "").strip()
        if v:
            return sanitize_client_id_for_path(v)
    try:
        local = getpass.getuser()
        if local:
            return sanitize_client_id_for_path(local)
    except Exception:
        pass
    try:
        host = socket.gethostname()
        if host:
            return sanitize_client_id_for_path(host)
    except Exception:
        pass
    return "unknown_client"


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


def default_virtuoso_bridge_dir(
    username: str,
    leaf: str,
    client_id: str | None = None,
) -> str:
    """Return the default remote scratch directory for bridge artifacts.

    When *client_id* is provided, include it as a path segment so the same
    remote user/profile pair can safely use a shared scratch root from
    multiple local machines.
    """
    safe = sanitize_username_for_path(username)
    root = remote_scratch_root()
    leaf_norm = leaf.strip("/").replace("\\", "/")
    if client_id:
        safe_client = sanitize_client_id_for_path(client_id)
        return f"{root}/virtuoso_bridge_{safe}/{safe_client}/{leaf_norm}"
    return f"{root}/virtuoso_bridge_{safe}/{leaf_norm}"

def private_dir_shell_prelude(label: str) -> str:
    """POSIX sh functions that create or verify directories private to the user.

    ``vb_ensure_dir DIR`` creates DIR with mode 700, or accepts an existing
    DIR only if it is a real directory (not a symlink) owned by the current
    user; a different owner fails, other modes are tightened to 700. On a
    shared /tmp this stops another user from pre-creating the path.
    """
    return rf"""umask 077
vb_uid=$(id -u) || exit $?
vb_read_meta() {{
    vb_meta=$(stat -c '%u %a' "$1" 2>/dev/null) ||
        vb_meta=$(stat -f '%u %Lp' "$1" 2>/dev/null) || return 74
    set -- $vb_meta
    vb_owner=$1
    vb_mode=$2
}}
vb_verify_dir() {{
    vb_path=$1
    if [ -L "$vb_path" ] || [ ! -d "$vb_path" ]; then
        printf 'unsafe remote {label} directory: %s\n' "$vb_path" >&2
        return 73
    fi
    vb_read_meta "$vb_path" || return $?
    if [ "$vb_owner" != "$vb_uid" ]; then
        printf 'remote {label} directory has wrong owner: %s\n' "$vb_path" >&2
        return 75
    fi
    if [ "$vb_mode" != 700 ]; then
        chmod 700 "$vb_path" || return $?
        if [ -L "$vb_path" ] || [ ! -d "$vb_path" ]; then
            printf 'unsafe remote {label} directory after chmod: %s\n' "$vb_path" >&2
            return 73
        fi
        vb_read_meta "$vb_path" || return $?
        if [ "$vb_owner" != "$vb_uid" ] || [ "$vb_mode" != 700 ]; then
            printf 'remote {label} directory is not private: %s\n' "$vb_path" >&2
            return 76
        fi
    fi
}}
vb_ensure_dir() {{
    vb_path=$1
    if [ -L "$vb_path" ]; then
        printf 'remote {label} directory is a symlink: %s\n' "$vb_path" >&2
        return 73
    fi
    if [ ! -e "$vb_path" ]; then
        mkdir -m 700 "$vb_path" || return $?
    fi
    vb_verify_dir "$vb_path"
}}
"""


def private_dirs_below(root: str, path: str) -> list[str]:
    """Each directory from just below *root* down to *path*, outermost first."""
    root = root.rstrip("/") or "/"
    path = path.rstrip("/")
    prefix = root if root.endswith("/") else root + "/"
    if not path.startswith(prefix):
        raise ValueError(f"{path!r} is not below {root!r}")
    parts = [part for part in path[len(prefix):].split("/") if part]
    return [prefix + "/".join(parts[: index + 1]) for index in range(len(parts))]


REMOTE_SPECTRE_LEAF = "virtuoso_bridge_spectre"


def default_remote_spectre_work_dir(
    username: str,
    client_id: str | None = None,
) -> str:
    """Default remote scratch for Spectre simulations."""
    return default_virtuoso_bridge_dir(username, REMOTE_SPECTRE_LEAF, client_id)

"""Remote scratch paths and username resolution for SSH uploads."""

from __future__ import annotations

import getpass
import os
import re
import socket
import time
from typing import TYPE_CHECKING

from virtuoso_bridge.env import load_vb_env

if TYPE_CHECKING:
    from virtuoso_bridge.transport.ssh import SSHRunner

REMOTE_SCRATCH_ROOT_ENV = "VB_REMOTE_SCRATCH_ROOT"
CLIENT_ID_ENV = "VB_CLIENT_ID"


def remote_scratch_root(runner: SSHRunner | None = None, *, timeout: float = 10) -> str:
    """Scratch parent (historically /tmp); expand ~ on the target host."""
    load_vb_env()
    root = os.environ.get(REMOTE_SCRATCH_ROOT_ENV, "").strip() or "/tmp"
    if root.startswith("~/") and runner is not None:
        home = getattr(runner, "_vb_remote_home", None)
        if not isinstance(home, str):
            result = runner.run_command('getent passwd "$(id -u)" | cut -d: -f6', timeout=timeout)
            home = result.stdout.strip()
            if result.returncode or not home.startswith("/"):
                raise ValueError("Cannot resolve the remote account home")
            runner._vb_remote_home = home
        root = home.rstrip("/") + root[1:]
    if not (root.startswith("/") or root.startswith("~/")) or ".." in root.split("/"):
        raise ValueError("VB_REMOTE_SCRATCH_ROOT must be absolute or start with ~/")
    return root.rstrip("/") or "/"


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
    timeout: float = 10,
) -> str:
    """Resolve SSH username: configured_user > whoami > getpass > fallback."""
    u = (configured_user or "").strip()
    if u:
        return sanitize_username_for_path(u)
    if runner is not None:
        whoami_result = runner.run_command("whoami", timeout=timeout)
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
    *,
    runner: SSHRunner | None = None,
    timeout: float = 10,
) -> str:
    """Return the default remote scratch directory for bridge artifacts.

    When *client_id* is provided, include it as a path segment so the same
    remote user/profile pair can safely use a shared scratch root from
    multiple local machines.
    """
    deadline = time.monotonic() + timeout
    username = username or resolve_remote_username(
        configured_user=getattr(runner, "user", getattr(runner, "_user", None)),
        runner=runner, timeout=timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Scratch path resolution deadline expired")
    root = remote_scratch_root(runner, timeout=remaining) + "/virtuoso_bridge_" + sanitize_username_for_path(username)
    leaf_norm = leaf.strip("/").replace("\\", "/")
    if client_id:
        safe_client = sanitize_client_id_for_path(client_id)
        return f"{root}/{safe_client}/{leaf_norm}"
    return f"{root}/{leaf_norm}"


def instance_scratch_root(runner: SSHRunner | None = None, *, timeout: float = 10) -> str:
    """Private registry root, independent of the legacy scratch parent.

    Preserve explicitly configured discovery roots from earlier preview builds.
    The shared /tmp default is only a parent, never a private registry itself.
    """
    deadline = time.monotonic() + timeout
    root = remote_scratch_root(runner, timeout=timeout)
    if root == "/tmp":
        if runner is None:
            user = resolve_remote_username(configured_user=None)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Instance root resolution deadline expired")
            result = runner.run_command("whoami", timeout=remaining)
            if result.returncode or not result.stdout.strip():
                raise ValueError("Cannot resolve the remote account for instance discovery")
            user = result.stdout.strip()
        return root + "/virtuoso_bridge_" + sanitize_username_for_path(user)
    return root


def private_staging_script(root: str, path: str, label: str) -> str:
    """Verify private children without changing the shared scratch parent."""
    import shlex

    directories = private_dirs_below(root, path)
    if not directories:
        raise ValueError("Staging needs a private child below the scratch parent")
    if root != "/tmp":
        directories.insert(0, root)
    return private_dir_shell_prelude(label) + "".join(
        "vb_ensure_root %s || exit $?\n" % shlex.quote(directory)
        for directory in directories
    )

def private_dir_shell_prelude(label: str) -> str:
    """POSIX sh functions that create or verify directories private to the user.

    ``vb_ensure_dir DIR`` creates DIR with mode 700, or accepts an existing
    DIR only if it is a real directory (not a symlink) owned by the current
    user; a different owner fails, other modes are tightened to 700. On a
    shared /tmp this stops another user from pre-creating the path.
    ``vb_ensure_root`` instead rejects unsafe existing permissions.
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
vb_ensure_root() {{
    vb_path=$1
    if [ ! -e "$vb_path" ] && [ ! -L "$vb_path" ]; then
        mkdir -m 700 "$vb_path" 2>/dev/null || [ -d "$vb_path" ] || return $?
    fi
    [ ! -L "$vb_path" ] && [ -d "$vb_path" ] || return 73
    vb_read_meta "$vb_path" || return $?
    if [ "$vb_owner" != "$vb_uid" ] || [ "$vb_mode" != 700 ]; then
        printf 'scratch root must be owned by this user with mode 700: %s\n' "$vb_path" >&2
        return 75
    fi
}}
vb_ensure_dir() {{
    vb_path=$1
    if [ -L "$vb_path" ]; then
        printf 'remote {label} directory is a symlink: %s\n' "$vb_path" >&2
        return 73
    fi
    if [ ! -e "$vb_path" ]; then
        mkdir -m 700 "$vb_path" 2>/dev/null || [ -d "$vb_path" ] || return $?
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
    *,
    runner: SSHRunner | None = None,
) -> str:
    """Default remote scratch for Spectre simulations."""
    return default_virtuoso_bridge_dir(username, REMOTE_SPECTRE_LEAF, client_id, runner=runner)

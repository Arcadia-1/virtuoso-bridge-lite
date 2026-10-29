"""SSHClient — SSH tunnel + remote RAMIC daemon deployment.

Manages the SSH port-forward tunnel and remote file deployment independently
from the SKILL execution client (VirtuosoClient). VirtuosoClient only needs a
localhost:port TCP endpoint; SSHClient makes that endpoint available.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import logging
import os
import re
import shlex
import shutil
import socket
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from virtuoso_bridge import daemon_auth
from virtuoso_bridge.env import load_vb_env, resolve_env_path
from virtuoso_bridge.profile import resolve_profile
from virtuoso_bridge.runtime_paths import legacy_cache_state_file, state_dir
from virtuoso_bridge.transport.remote_paths import (
    REMOTE_SCRATCH_ROOT_ENV,
    default_virtuoso_bridge_dir,
    instance_scratch_root,
    private_dir_shell_prelude,
    private_dirs_below,
    remote_scratch_root,
    resolve_client_id,
    resolve_remote_username,
)
from virtuoso_bridge.transport.remote_roles import remote_host_roles_from_os
from virtuoso_bridge.transport.ssh import (
    CommandResult,
    SSHRunner,
    _pid_is_alive,
    ssh_backend_env_from_os,
    ssh_proxy_url_from_os,
    verbose_from_env,
)

logger = logging.getLogger(__name__)

_TUNNEL_STARTUP_SETTLE_SECONDS = 1.0


def _is_localhost(host: str | None) -> bool:
    """Return True if *host* refers to the local machine."""
    if not host:
        return False
    return host.strip().lower() in ("localhost", "127.0.0.1", "::1")


def _tunnel_target(user: str | None, host: str | None, remote_port: Any) -> str | None:
    """``user@host:port`` naming the daemon a tunnel leads to, or None."""
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return None
    user = (user or "").strip()
    try:
        port = f":{int(remote_port)}" if remote_port not in (None, "") else ""
    except (TypeError, ValueError):
        port = ""
    return f"{user + '@' if user else ''}{host}{port}"


def _configured_target(profile: str | None = None) -> str | None:
    """The tunnel target of the configuration already loaded into os.environ.

    Resolved like :meth:`SSHClient.from_env`, so two ``.env`` files for
    different hosts, users or daemon ports name different tunnels.
    """
    roles = remote_host_roles_from_os(profile, load=False)
    host = roles.daemon_host or roles.legacy_host
    if not host:
        return None
    from virtuoso_bridge.virtuoso.basic.bridge import _default_remote_port

    suffix = f"_{profile}" if profile else ""
    try:
        port = int(os.getenv(f"VB_REMOTE_PORT{suffix}", "").strip() or _default_remote_port(roles.remote_user))
    except (TypeError, ValueError):
        port = _default_remote_port(roles.remote_user)
    return _tunnel_target(roles.remote_user, host, port)


def _state_target_matches(state: dict[str, Any], target: str) -> bool:
    """Whether a saved state describes *target*.

    Remote states must record a host and port, even when their filename or
    target field already matches. Older local-only states remain readable.
    """
    if state.get("target") and state["target"] != target:
        return False
    if state.get("mode") == "remote":
        if type(state.get("remote_port")) is not int or not 1 <= state["remote_port"] <= 65535:
            return False
        if not (state.get("daemon_host") or state.get("remote_host")):
            return False
    saved = _tunnel_target(
        state.get("remote_user"),
        str(state.get("daemon_host") or state.get("remote_host") or ""),
        state.get("remote_port"),
    )
    if saved is None:
        return True
    if "@" not in saved and "@" in target:
        target = target.split("@", 1)[1]
    if ":" not in saved and ":" in target:
        target = target.rsplit(":", 1)[0]
    return saved == target


def _state_file(profile: str | None = None, target: str | None = None) -> Path:
    """State of the tunnel for *profile* and *target*.

    Each target keeps its own file, so ``.env`` files for different hosts
    can run tunnels side by side without a profile. The file without a
    target is the format earlier versions wrote.
    """
    name = f"state_{profile}" if profile else "state"
    if target:
        name += "-" + re.sub(r"[^A-Za-z0-9@._-]", "_", target)
    return state_dir() / f"{name}.json"


def _legacy_state_files(profile: str | None = None) -> list[Path]:
    primary = _state_file(profile)
    legacy = legacy_cache_state_file(profile)
    return [primary] if primary == legacy else [primary, legacy]


_CONFIGURED: Any = object()


def _selection_file(profile: str | None = None) -> Path:
    roles = remote_host_roles_from_os(profile, load=False)
    suffix = f"_{profile}" if profile else ""
    key = json.dumps([roles.daemon_host, roles.remote_user, profile,
                      str(resolve_env_path() or ""), os.getenv(f"VB_REMOTE_WORK_DIR{suffix}", ""),
                      os.getenv(f"VB_REMOTE_PORT{suffix}", ""), remote_scratch_root()])
    return state_dir() / ("instance-" + hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")


def _read_selection(profile: str | None = None) -> dict:
    from virtuoso_bridge.instance_discovery import InstanceSelectionError, validate_record
    try:
        value = _read_private_json(_selection_file(profile))
        if not isinstance(value, dict):
            raise ValueError("selection must be an object")
        record = validate_record(value.get("record"))
        if type(value.get("port")) is not int or value["port"] != record["port"]:
            raise ValueError("selection port differs from its record")
        roles = remote_host_roles_from_os(profile, load=False)
        if value.get("target") != _tunnel_target(roles.remote_user, roles.daemon_host, record["port"]):
            raise ValueError("selection target differs from configuration")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise InstanceSelectionError("Invalid saved instance selection at %s: %s. "
                                     "Inspect this file before selecting again." % (_selection_file(profile), exc)) from exc


def _read_private_json(path: Path, *, legacy_metadata: bool = False) -> Any:
    """Read only a private regular state file, checking the opened inode.

    Windows relies on the user's protected state directory/ACLs. POSIX mode
    checks are deliberately not presented as Windows ACL validation.
    """
    # Absence is not unsafe input, even when an old installation left the
    # directory permissive. Validate its permissions before reading any file.
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise ValueError("State must be a regular file, not a symlink")
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or path.parent.is_symlink():
        raise ValueError("State parent must be a real directory")
    mask = 0o022 if legacy_metadata else 0o077
    if legacy_metadata and os.name == "posix":
        for ancestor in path.absolute().parents:
            metadata = ancestor.lstat()
            trusted_sticky = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
            if (not stat.S_ISDIR(metadata.st_mode) or ancestor.is_symlink()
                    or metadata.st_uid not in (0, os.geteuid())
                    or (metadata.st_mode & 0o022 and not trusted_sticky)):
                raise ValueError("Unsafe connection-state ancestor: " + str(ancestor))
    if os.name == "posix" and (parent.st_uid != os.geteuid() or parent.st_mode & mask):
        raise ValueError("State directory must belong to this user with private permissions")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("State file changed while opening")
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & mask):
            raise ValueError("State file must belong to this user with private permissions")
        if info.st_size > 1024 * 1024:
            raise ValueError("State file is too large")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            value = json.load(stream)
        if legacy_metadata and os.name == "posix" and (parent.st_mode | info.st_mode) & 0o077:
            # Only the non-secret schema written before instance discovery may
            # use historical umask-022 permissions. Never apply this exception
            # to signed selections, credentials or new process identity state.
            fields = {"mode", "port", "remote_port", "tunnel_pid", "target", "remote_user",
                      "remote_host", "daemon_host", "deploy_host", "gui_host", "spectre_host",
                      "daemon_endpoint_hostname", "setup_path", "identity_path", "profile", "started_at"}
            if not isinstance(value, dict) or not set(value).issubset(fields):
                raise ValueError("Permissive permissions are allowed only for historical connection metadata")
            _validate_state(value)
        return value
    finally:
        if fd >= 0:
            os.close(fd)


def _validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("mode") not in ("local", "remote"):
        raise ValueError("Invalid state object/mode")
    if type(state.get("port")) is not int or not 1 <= state["port"] <= 65535:
        raise ValueError("Invalid local port")
    if state["mode"] == "remote":
        if (type(state.get("remote_port")) is not int or not 1 <= state["remote_port"] <= 65535
                or type(state.get("tunnel_pid")) is not int or state["tunnel_pid"] <= 0
                or not isinstance(state.get("daemon_host") or state.get("remote_host"), str)
                or not (state.get("daemon_host") or state.get("remote_host"))
                or (state.get("remote_user") is not None and not isinstance(state["remote_user"], str))):
            raise ValueError("Invalid saved tunnel endpoint/PID")
    if state.get("instance_record") is not None:
        from virtuoso_bridge.instance_discovery import validate_record
        record = validate_record(state["instance_record"])
        if record["port"] != state.get("remote_port", state["port"]):
            raise ValueError("State port differs from its record")
    target = state.get("target")
    if target is not None and (not isinstance(target, str) or not _state_target_matches(state, target)):
        raise ValueError("State target differs from its endpoint")
    return state


def _write_private_json(path: Path, value: dict) -> None:
    """Publish one complete private state without discarding its predecessor."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        if path.parent.is_symlink() or path.parent.stat().st_uid != os.geteuid():
            raise ValueError("State directory must belong to this user and not be a symlink")
        path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers and concurrent replacers can briefly hold a handle
        # without FILE_SHARE_DELETE. Keep the old complete file until publish.
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 7:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        Path(temporary).unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Resource helpers (moved from bridge.py)
# ---------------------------------------------------------------------------

def _find_ramic_bridge_il() -> Path:
    try:
        resources = importlib.resources.files("virtuoso_bridge.virtuoso.basic.resources")
        il_ref = resources / "ramic_bridge.il"
        with importlib.resources.as_file(il_ref) as il_path:
            if il_path.is_file():
                return il_path
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass
    raise FileNotFoundError(
        "Cannot locate ramic_bridge.il in virtuoso_bridge.virtuoso.basic.resources."
    )


def _find_ramic_bridge_daemon(python_major: int) -> Path:
    filename = "ramic_bridge_daemon_3.py" if python_major >= 3 else "ramic_bridge_daemon_27.py"
    try:
        resources = importlib.resources.files("virtuoso_bridge.virtuoso.basic.resources")
        daemon_ref = resources / filename
        with importlib.resources.as_file(daemon_ref) as daemon_path:
            if daemon_path.is_file():
                return daemon_path
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass
    raise FileNotFoundError(f"Cannot locate {filename} in virtuoso_bridge.virtuoso.basic.resources.")


def _find_ramic_helper(filename: str) -> Path:
    try:
        resources = importlib.resources.files("virtuoso_bridge.virtuoso.basic.resources")
        helper_ref = resources / filename
        with importlib.resources.as_file(helper_ref) as helper_path:
            if helper_path.is_file():
                return helper_path
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass
    raise FileNotFoundError(
        f"Cannot locate {filename} in virtuoso_bridge.virtuoso.basic.resources."
    )



def _generate_virtuoso_setup_il(
    daemon_path: str,
    il_path: str,
    python_cmd: str = "python",
    port: int = 65432,
    identity_path: str | None = None,
    listen_all: bool = False,
    scratch_root: str | None = None,
) -> str:
    from virtuoso_bridge.virtuoso.ops import escape_skill_string
    scratch_line = (f'setShellEnvVar("RB_SCRATCH_ROOT" "{escape_skill_string(scratch_root)}")\n'
                    if scratch_root else "")
    identity_line = (
        f'setShellEnvVar("RB_IDENTITY_PATH" "{identity_path}")\n'
        if identity_path
        else 'setShellEnvVar("RB_IDENTITY_PATH" "")\n'
    )
    return (
        "; Virtuoso CIW Setup Script - RAMIC Bridge\n"
        "; Auto-generated by virtuoso-bridge\n"
        f'; Execute in CIW: load("{il_path.replace(chr(0x5c), "/")}")\n'
        "\n"
        "; Set environment variables\n"
        f'setShellEnvVar("RB_DAEMON_PATH" "{daemon_path}")\n'
        f'setShellEnvVar("RB_PYTHON_PATH" "{python_cmd}")\n'
        f'setShellEnvVar("RB_PORT" "{port}")\n'
        f'setShellEnvVar("RB_AUTO_PORT" "{1 if port == 0 else 0}")\n'
        f"{scratch_line}"
        f'setShellEnvVar("RB_LISTEN_ALL" "{1 if listen_all else 0}")\n'
        f"{identity_line}"
        "\n"
        "; Load RAMIC Bridge IL script\n"
        f'load("{il_path}")\n'
        "\n"
        "; Setup complete!\n"
    )


_SETUP_PORT_RE = re.compile(r'^setShellEnvVar\("RB_PORT" "(\d+)"\)$', re.MULTILINE)


def _setup_port(text: str) -> int | None:
    """Daemon port that a generated virtuoso_setup.il starts, if any."""
    match = _SETUP_PORT_RE.search(text or "")
    return int(match.group(1)) if match else None


def _update_env_file(key: str, value: str) -> bool:
    try:
        env_path = resolve_env_path()
    except FileNotFoundError:
        return False
    if env_path is not None and env_path.is_file():
        text = env_path.read_text(encoding="utf-8")
        new_text = re.sub(
            rf"^{re.escape(key)}\s*=.*$",
            f"{key}={value}",
            text,
            flags=re.MULTILINE,
        )
        if new_text != text:
            env_path.write_text(new_text, encoding="utf-8")
            logger.info("Updated %s=%s in %s", key, value, env_path)
            return True
        return False
    return False


def _profiled_bridge_leaf(profile: str | None) -> str:
    """Remote bridge directory leaf for a connection profile."""
    if not profile:
        return "virtuoso_bridge"
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", profile.strip())[:64]
    return f"virtuoso_bridge_{safe or 'profile'}"


def _profiled_env_key(base: str, profile: str | None) -> str:
    return f"{base}_{profile}" if profile else base


def _remote_port_occupancy_cmd(port: int) -> str:
    """POSIX-sh one-liner classifying who holds *port* on the remote host.

    Returns FREE (nothing listening), OWN (a bridge daemon of the current
    SSH user — its argv ends with the port), or FOREIGN (something else,
    typically another user's Virtuoso bridge daemon).  ``ss`` is probed
    first, ``netstat`` is the fallback; when neither exists the command
    prints FREE and the caller keeps the default port.
    """
    return (
        f"if (ss -tln 2>/dev/null || netstat -tln 2>/dev/null) "
        f"| awk '{{print $4}}' | grep -E ':{port}$' >/dev/null 2>&1; then "
        f"if pgrep -u \"$(id -un 2>/dev/null)\" -f "
        f"'ramic_bridge_daemon_(3|27)\\.py.* {port}$' >/dev/null 2>&1; then "
        f"echo OWN; else echo FOREIGN; fi; else echo FREE; fi"
    )


# ---------------------------------------------------------------------------
# SSHClient
# ---------------------------------------------------------------------------

class SSHClient:
    """Manages SSH tunnel and remote RAMIC daemon deployment.

    Provides a localhost:port TCP endpoint that tunnels to the remote daemon.
    Completely independent of SKILL execution logic.
    """

    def __init__(
        self,
        remote_host: str,
        remote_user: str | None = None,
        port: int = 65432,
        local_port: int | None = None,
        jump_host: str | None = None,
        jump_user: str | None = None,
        timeout: int = 30,
        keep_remote_files: bool = False,
        profile: str | None = None,
        ssh_backend: str | None = None,
        ssh_max_sessions: int | None = None,
        ssh_proxy_url: str | None = None,
        daemon_host: str | None = None,
        deploy_host: str | None = None,
        gui_host: str | None = None,
        spectre_host: str | None = None,
        listen_all: bool = False,
        discover_instances: bool = False,
        expected_work_dir: str = "",
        selected_port: int | None = None,
        shared_setup: str = "",
        replace_setup: bool = False,
    ) -> None:
        self._daemon_host = daemon_host or remote_host
        self._listen_all = listen_all
        self._discover_instances = discover_instances
        self._legacy_fallback = False
        self._legacy_port: int | None = None
        self._expected_work_dir = expected_work_dir
        self._selected_port = selected_port
        self._shared_setup = shared_setup
        self._replace_setup = replace_setup
        self.instance_record: dict | None = None
        self._gui_host = gui_host or remote_host
        self._deploy_host = deploy_host or self._gui_host
        self._spectre_host = spectre_host or remote_host
        # Compatibility: remote_host has historically meant the tunnel target.
        self._remote_host = self._daemon_host
        self._remote_user = remote_user
        self._port = port  # remote daemon port
        self._local_port = local_port if local_port is not None else port
        self._jump_host = jump_host
        self._jump_user = jump_user
        self._timeout = timeout
        self._keep_remote_files = keep_remote_files
        self._profile = profile

        self._runner_kwargs = {
            "jump_user": jump_user,
            "backend": ssh_backend,
            "max_sessions": ssh_max_sessions,
            "proxy_url": ssh_proxy_url,
            "verbose": verbose_from_env(),
        }
        self._role_runners: dict[str, SSHRunner] = {}

        if _is_localhost(self._daemon_host):
            self._ssh_runner = None
        else:
            self._ssh_runner = self._new_role_runner(
                self._daemon_host,
                role="daemon",
                user=remote_user,
            )

        if _is_localhost(self._deploy_host):
            self._deployment_runner = None
        elif self._deploy_host == self._daemon_host:
            self._deployment_runner = self._ssh_runner
        else:
            self._deployment_runner = self._new_role_runner(
                self._deploy_host,
                role="deploy",
                user=remote_user,
            )

        self._remote_setup_done = False
        self._remote_work_dir: str | None = None
        self._remote_virtuoso_setup_path: str | None = None
        self._replaced_setup_port: int | None = None
        self._remote_identity_path: str | None = None
        self._daemon_endpoint_hostname: str | None = None
        self._daemon_token: str | None = None
        self._daemon_token_from_cache = False
        self._tunnel_endpoint: tuple[int, int, int | None] | None = None

    def _new_role_runner(
        self,
        host: str,
        *,
        role: str,
        user: str | None = None,
    ) -> SSHRunner:
        jump_host = self._jump_host
        if jump_host and host.strip().rstrip(".").lower() == jump_host.strip().rstrip(".").lower():
            jump_host = None
        runner = SSHRunner(
            host=host,
            user=user if user is not None else self._remote_user,
            jump_host=jump_host,
            persistent_shell=True,
            **self._runner_kwargs,
        )
        self._role_runners[role] = runner
        return runner

    def _runner_for_role(self, role: str, host: str | None) -> SSHRunner | None:
        if not host or _is_localhost(host):
            return None
        if host == self._daemon_host:
            return self._ssh_runner
        if host == self._deploy_host and self._deploy_host != self._daemon_host:
            return self._deployment_runner
        existing = self._role_runners.get(role)
        if existing is not None:
            return existing
        for runner in self._role_runners.values():
            if runner.host.strip().rstrip(".").lower() == host.strip().rstrip(".").lower():
                self._role_runners[role] = runner
                return runner
        return self._new_role_runner(host, role=role)

    # -- factory methods ----------------------------------------------------

    @classmethod
    def from_env(cls, *, keep_remote_files: bool = False, profile: str | None = None,
                 selected_port: int | None = None, read_selection: bool = True,
                 replace_setup: bool = False) -> "SSHClient":
        """Create from VB_* environment variables.

        If *profile* is given (e.g. ``"gpu1"``), reads ``VB_REMOTE_HOST_gpu1``
        etc.  Otherwise resolves a profile binding before falling back to the
        default unsuffixed variables.
        """
        profile = resolve_profile(profile)
        load_vb_env()

        suffix = f"_{profile}" if profile else ""

        roles = remote_host_roles_from_os(profile, load=False)
        remote_host = roles.legacy_host or roles.daemon_host
        if not remote_host or not roles.daemon_host or not roles.deploy_host:
            raise RuntimeError(
                f"No remote host is configured for profile {profile or '(default)'!r}; "
                f"set VB_REMOTE_HOST{suffix}, or set explicit VB_GUI_HOST{suffix} "
                f"and VB_DAEMON_HOST{suffix} roles"
            )

        remote_user = roles.remote_user
        jump_host = roles.jump_host
        jump_user = roles.jump_user
        backend_env = ssh_backend_env_from_os(profile)
        ssh_proxy_url = ssh_proxy_url_from_os(profile)

        # Port
        from virtuoso_bridge.virtuoso.basic.bridge import _default_remote_port
        configured_port = os.getenv(f"VB_REMOTE_PORT{suffix}", "").strip()
        legacy_port = int(configured_port) if configured_port else None
        if legacy_port is not None and not 1 <= legacy_port <= 65535:
            raise ValueError(f"VB_REMOTE_PORT{suffix} must be between 1 and 65535")
        if selected_port is not None and not 1 <= selected_port <= 65535:
            raise ValueError(f"VB_REMOTE_PORT{suffix} must be between 1 and 65535")
        port = selected_port or legacy_port or _default_remote_port(remote_user)
        from virtuoso_bridge.instance_discovery import validate_selector
        expected_work_dir = os.getenv(f"VB_REMOTE_WORK_DIR{suffix}", "").strip()
        validate_selector(expected_work_dir, selected_port)

        # Local port (defaults to remote port if not set)
        local_port_str = os.getenv(f"VB_LOCAL_PORT{suffix}", "").strip()
        local_port: int | None = None
        if local_port_str:
            try:
                local_port = int(local_port_str)
            except (ValueError, TypeError):
                pass

        selection = _read_selection(profile) if read_selection else {}
        if selected_port is None or selection.get("port") == selected_port:
            port = selection.get("port", port)
        client = cls(
            remote_host=remote_host,
            remote_user=remote_user,
            port=port,
            local_port=local_port,
            jump_host=jump_host,
            jump_user=jump_user,
            keep_remote_files=keep_remote_files,
            profile=profile,
            ssh_backend=backend_env.backend,
            ssh_max_sessions=backend_env.max_sessions,
            ssh_proxy_url=ssh_proxy_url,
            daemon_host=roles.daemon_host,
            deploy_host=roles.deploy_host,
            gui_host=roles.gui_host,
            spectre_host=roles.spectre_host,
            listen_all=os.getenv(f"VB_DAEMON_LISTEN_ALL{suffix}", "").strip() == "1",
            discover_instances=True,
            selected_port=selected_port,
            expected_work_dir=expected_work_dir,
            shared_setup=os.getenv(f"VB_DAEMON_SETUP{suffix}", "").strip(),
            replace_setup=replace_setup,
        )
        client.instance_record = selection.get("record")
        client._legacy_port = legacy_port
        return client

    # -- properties ---------------------------------------------------------

    @property
    def port(self) -> int:
        """Port that VirtuosoClient should connect to.

        In local mode there is no SSH tunnel, so the client connects
        directly to the daemon port.  In remote mode the client connects
        to the local end of the SSH tunnel.
        """
        if _is_localhost(self._remote_host):
            return self._port
        return self._local_port

    @property
    def remote_port(self) -> int:
        """Port used by the bridge daemon on the remote endpoint."""
        return self._port

    @property
    def expected_work_dir(self) -> str:
        return self._expected_work_dir

    @property
    def selected_port(self) -> int | None:
        return self._selected_port

    @property
    def remote_user(self) -> str | None:
        return self._remote_user

    @property
    def jump_host(self) -> str | None:
        return self._jump_host

    @property
    def ssh_backend(self) -> str:
        return self._ssh_runner.backend if self._ssh_runner is not None else "local"

    @property
    def remote_host(self) -> str:
        return self._remote_host

    @property
    def daemon_host(self) -> str:
        return self._daemon_host

    @property
    def deploy_host(self) -> str:
        return self._deploy_host

    @property
    def gui_host(self) -> str:
        return self._gui_host

    @property
    def spectre_host(self) -> str:
        return self._spectre_host

    @property
    def ssh_runner(self) -> SSHRunner | None:
        """Runner for the daemon/tunnel endpoint (legacy property)."""
        return self._ssh_runner

    @property
    def deployment_runner(self) -> SSHRunner | None:
        """Runner used for bridge files and Virtuoso-generated artifacts."""
        if self._deploy_host == self._daemon_host:
            return self._ssh_runner
        return self._deployment_runner

    @property
    def gui_runner(self) -> SSHRunner | None:
        """Runner for X11/CIW operations."""
        return self._runner_for_role("gui", self._gui_host)

    @property
    def spectre_runner(self) -> SSHRunner | None:
        """Runner for standalone Spectre jobs."""
        return self._runner_for_role("spectre", self._spectre_host)

    def _require_runner(self) -> SSHRunner:
        runner = self._ssh_runner
        if runner is None:
            raise RuntimeError("SSH runner is unavailable in local mode")
        return runner

    def _require_deployment_runner(self) -> SSHRunner:
        runner = self.deployment_runner
        if runner is None:
            raise RuntimeError("Deployment SSH runner is unavailable in local mode")
        return runner

    @property
    def remote_work_dir(self) -> str | None:
        return self._remote_work_dir

    @property
    def setup_path(self) -> str | None:
        return self._remote_virtuoso_setup_path

    @property
    def replaced_setup_port(self) -> int | None:
        """Port of the setup file this deployment replaced, when it differed."""
        return self._replaced_setup_port

    @property
    def identity_path(self) -> str | None:
        return self._remote_identity_path

    @property
    def daemon_token(self) -> str | None:
        return self._daemon_token

    def _token_unavailable(self, message: str) -> None:
        """Fail loudly about an unusable bridge token unless the explicit
        insecure-legacy opt-in (``VB_ALLOW_UNAUTHENTICATED_DAEMON=1``) is set."""
        if daemon_auth.allow_unauthenticated():
            logger.warning(
                "%s Continuing without a token (explicit %s=1 opt-in).",
                message, daemon_auth.UNAUTH_OPTIN_ENV,
            )
            return
        raise daemon_auth.DaemonTokenError(
            f"{message} The bridge refuses to run without token "
            f"authentication by default; fix token provisioning, or set "
            f"{daemon_auth.UNAUTH_OPTIN_ENV}=1 to explicitly accept "
            f"unauthenticated legacy mode."
        )

    def remote_cache_path(self) -> Path | None:
        """Local cache of this daemon's token and verified identity (remote only)."""
        if self._ssh_runner is None:
            return None
        return daemon_auth.remote_cache_path(self._daemon_host, self._remote_user)

    @property
    def daemon_token_from_cache(self) -> bool:
        """Whether the current token came from the local cache, not over SSH."""
        return self._daemon_token_from_cache

    def refresh_daemon_token(self) -> str | None:
        """Drop the cached token and identity, then fetch the token over SSH."""
        cache = self.remote_cache_path()
        if cache is not None:
            daemon_auth.update_remote_cache(cache, token=None, verified=None)
        self._daemon_token = None
        self._daemon_token_from_cache = False
        return self.ensure_daemon_token()

    def _adopt_fetched_token(self, token: str) -> str:
        self._daemon_token = token
        self._daemon_token_from_cache = False
        cache = self.remote_cache_path()
        if cache is not None:
            daemon_auth.update_remote_cache(cache, token=token)
        return token

    def read_daemon_token(self) -> str | None:
        """Read the existing credential without provisioning or cache writes."""
        if self._ssh_runner is None:
            return daemon_auth.read_local_token()
        result = self._ssh_runner.run_command("cat ~/.virtuoso-bridge/bridge_token 2>/dev/null")
        token = result.stdout.strip() if result.returncode == 0 else None
        return token.lower() if daemon_auth.is_valid_token(token) else None

    def ensure_daemon_token(self) -> str | None:
        """Fetch (or create over SSH) the bridge daemon auth token.

        The token lives in ``~/.virtuoso-bridge/bridge_token`` (atomic 0600
        file under a 0700 directory) on the daemon's machine and only ever
        travels over this authenticated SSH channel — it is the shared secret
        that lets clients and their own user's daemon recognize each other
        (see :mod:`virtuoso_bridge.daemon_auth`).  Provisioning failures are
        fatal by default (:class:`daemon_auth.DaemonTokenError`); they degrade
        to returning None only under the explicit
        ``VB_ALLOW_UNAUTHENTICATED_DAEMON=1`` opt-in.
        """
        if self._daemon_token and daemon_auth.is_valid_token(self._daemon_token):
            return self._daemon_token
        if self._ssh_runner is None:
            self._daemon_token = daemon_auth.local_token_or_raise()
            return self._daemon_token
        # A cached copy saves the SSH round trip; a client whose handshake
        # rejects it calls refresh_daemon_token().
        cache = self.remote_cache_path()
        cached = daemon_auth.read_remote_cache(cache).get("token") if cache else None
        if isinstance(cached, str) and daemon_auth.is_valid_token(cached):
            self._daemon_token = cached.lower()
            self._daemon_token_from_cache = True
            return self._daemon_token
        runner = self._ssh_runner
        try:
            result = runner.run_command(
                "cat ~/.virtuoso-bridge/bridge_token 2>/dev/null"
            )
            existing = (result.stdout or "").strip()
            if result.returncode == 0 and daemon_auth.is_valid_token(existing):
                return self._adopt_fetched_token(existing.lower())
            # Provision a fresh token through the SSH channel.  Stage a
            # complete 0600 file, then hard-link it into place only if the
            # target is still absent.  Every concurrent starter reads back
            # and adopts the winner instead of overwriting it with mv -f.
            token = daemon_auth.generate_token()
            home = (
                runner.run_command('printf %s "$HOME"').stdout or ""
            ).strip()
            if not home:
                self._token_unavailable("Cannot resolve remote $HOME for bridge token")
                return None
            token_dir = f"{home}/.virtuoso-bridge"
            token_path = f"{token_dir}/bridge_token"
            mkdir = runner.run_command(
                f"mkdir -p '{token_dir}' && chmod 700 '{token_dir}'"
            )
            if mkdir.returncode != 0:
                self._token_unavailable(
                    f"Cannot create {token_dir} for bridge token: "
                    f"{mkdir.stderr.strip()}"
                )
                return None
            tmp_path = (
                f"{token_path}.tmp.{os.getpid()}."
                f"{daemon_auth.generate_token()[:16]}"
            )
            upload = runner.upload_text(token + "\n", tmp_path)
            if upload.returncode != 0:
                self._token_unavailable(
                    f"Cannot upload bridge token: {upload.stderr.strip()}"
                )
                return None
            tmp_q = shlex.quote(tmp_path)
            token_q = shlex.quote(token_path)
            finalize = runner.run_command(
                f"chmod 600 {tmp_q} && "
                f"(ln {tmp_q} {token_q} 2>/dev/null || test -r {token_q}) && "
                f"rm -f {tmp_q} && chmod 600 {token_q} && cat {token_q}"
            )
            disk_token = (finalize.stdout or "").strip()
            if finalize.returncode != 0 or not daemon_auth.is_valid_token(disk_token):
                runner.run_command(f"rm -f {tmp_q}")
                detail = finalize.stderr.strip() or "installed token is unreadable or invalid"
                self._token_unavailable(
                    f"Cannot finalize bridge token at {token_path}: "
                    f"{detail}"
                )
                return None
            logger.info("Provisioned bridge daemon token at %s", token_path)
            return self._adopt_fetched_token(disk_token.lower())
        except daemon_auth.DaemonTokenError:
            raise
        except Exception as exc:
            self._token_unavailable(f"Bridge token provisioning failed: {exc}")
            return None

    @property
    def is_tunnel_alive(self) -> bool:
        if self._ssh_runner is None:
            return False
        return self._active_tunnel_matches()

    # -- remote deployment --------------------------------------------------

    def _detect_remote_python(self, runner: SSHRunner | None = None, *, timeout: float | None = None) -> tuple[str, int, int]:
        # Try Cadence-bundled Python 3.9+ first (IC23.1+), then system python3,
        # then generic python, then python2.7.
        detect_cmd = (
            '(test -x "$CDSHOME/tools.lnx86/python/64bit/bin/python3" && '
            '$CDSHOME/tools.lnx86/python/64bit/bin/python3 --version 2>&1 && '
            'echo "CMD:$CDSHOME/tools.lnx86/python/64bit/bin/python3") || '
            '(python3 --version 2>&1 && echo "CMD:python3") || '
            '(python --version 2>&1 && echo "CMD:python") || '
            '(python2.7 --version 2>&1 && echo "CMD:python2.7") || '
            'echo "CMD:NONE"'
        )
        runner = runner or self._require_runner()
        result = runner.run_command(detect_cmd, **({"timeout": timeout} if timeout is not None else {}))
        output = result.stdout.strip()
        stderr = result.stderr.strip()
        logger.info("Remote Python detection output: %s", output)

        python_cmd = None
        python_major = None
        python_minor = 0
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("CMD:"):
                cmd = line[4:]
                if cmd != "NONE":
                    python_cmd = cmd
            elif line.startswith("Python "):
                try:
                    parts = line.split()[1].split(".")
                    python_major = int(parts[0])
                    python_minor = int(parts[1]) if len(parts) > 1 else 0
                except (IndexError, ValueError):
                    pass

        if python_cmd is None or python_major is None:
            details = f"Detection output: {output!r}"
            if stderr:
                details += f". SSH stderr: {stderr!r}"
            if result.returncode != 0:
                details += f". SSH return code: {result.returncode}"
            raise RuntimeError(
                f"No Python interpreter found on {self._daemon_host} (daemon host). "
                f"{details}"
            )
        logger.info("Detected remote Python: %s (version %d.%d)", python_cmd, python_major, python_minor)
        return python_cmd, python_major, python_minor

    def _deconflict_remote_port(self, runner: SSHRunner) -> bool:
        """Shift ``self._port`` off any port held by another user's process.

        The daemon port is a host-global, first-come-first-served resource:
        whoever binds it first owns it, and the SSH tunnel lands on their
        Virtuoso no matter who we logged in as.  When the configured port is
        held by a foreign listener, walk upward for a port that is free or
        held by one of this user's own bridge daemons.  The chosen port is
        written back to the .env so the mapping is stable.

        Probe failures never block startup (the identity guard in
        daemon_guard is the hard stop); returns True when the port changed.
        """
        port = self._port
        for _ in range(40):
            try:
                result = runner.run_command(_remote_port_occupancy_cmd(port))
            except Exception as exc:
                logger.warning("Remote port occupancy probe failed; keeping port %d: %s", port, exc)
                return False
            stdout = (result.stdout or "").strip()
            verdict = stdout.splitlines()[-1].strip() if stdout else ""
            if verdict not in ("FREE", "OWN", "FOREIGN"):
                logger.warning(
                    "Remote port occupancy probe returned %r; keeping port %d", verdict, port
                )
                return False
            if verdict != "FOREIGN":
                break
            logger.info("Remote port %d held by another user; probing %d", port, port + 1)
            port += 1
        else:
            logger.warning(
                "No foreign-free remote port found probing %d-%d; keeping %d",
                self._port, port, self._port,
            )
            return False
        if port == self._port:
            return False
        old_port = self._port
        self._port = port
        print(
            f"[port] remote port {old_port} in use by another user, "
            f"auto-switched to {port}",
            flush=True,
        )
        _update_env_file(_profiled_env_key("VB_REMOTE_PORT", self._profile), str(port))
        return True

    def ensure_remote_setup(self) -> None:
        """Upload daemon files and generate virtuoso_setup.il on the remote host."""
        if self._remote_setup_done:
            return

        runner = self._require_deployment_runner()
        daemon_runner = self._require_runner()
        if self._discover_instances:
            python_cmd, python_major, _ = self._detect_remote_python()
            self._publish_generated_setup(runner, python_cmd, python_major)
            return
        self._deconflict_remote_port(daemon_runner)
        python_cmd, python_major, _python_minor = self._detect_remote_python()
        daemon_local = _find_ramic_bridge_daemon(
            3 if python_major >= 3 else 2
        )
        recovery_local = _find_ramic_helper("ramic_request_recovery.py")
        owner_local = _find_ramic_helper("ramic_owner.py")
        registry_local = _find_ramic_helper("ramic_registry.py")
        il_local = _find_ramic_bridge_il()
        daemon_filename = daemon_local.name

        remote_username = resolve_remote_username(
            configured_user=self._remote_user,
            runner=runner,
        )
        self._remote_work_dir = default_virtuoso_bridge_dir(
            remote_username,
            _profiled_bridge_leaf(self._profile),
            resolve_client_id(self._profile),
        )

        remote_daemon = f"{self._remote_work_dir}/{daemon_filename}"
        remote_recovery = f"{self._remote_work_dir}/{recovery_local.name}"
        remote_owner = f"{self._remote_work_dir}/{owner_local.name}"
        remote_il = f"{self._remote_work_dir}/ramic_bridge.il"
        remote_setup = f"{self._remote_work_dir}/virtuoso_setup.il"
        remote_identity = f"{self._remote_work_dir}/daemon_identity.txt"

        # The CIW loads (and .cdsinit may autoload) code from this directory,
        # so every level below the scratch root must be ours and private.
        logger.info("Creating remote directory: %s", self._remote_work_dir)
        scratch_root = remote_scratch_root()
        mkdir_result = runner.run_command(
            private_dir_shell_prelude("bridge")
            + "".join(
                f"vb_ensure_dir {shlex.quote(directory)} || exit $?\n"
                for directory in private_dirs_below(scratch_root, self._remote_work_dir)
            )
            + f"rm -f {shlex.quote(remote_identity)}\n"
        )
        if mkdir_result.returncode != 0:
            raise RuntimeError(
                f"Refusing to deploy the bridge into {self._remote_work_dir}: "
                f"{mkdir_result.stderr.strip() or f'exit status {mkdir_result.returncode}'}. "
                f"Set {REMOTE_SCRATCH_ROOT_ENV} to a directory only you can write."
            )

        logger.info("Uploading daemon script (%s) to %s", daemon_filename, remote_daemon)
        daemon_content = daemon_local.read_text(encoding="utf-8")
        up = runner.upload_text(daemon_content, remote_daemon)
        if up.returncode != 0:
            raise RuntimeError(f"Failed to upload daemon: {up.stderr.strip()}")

        logger.info("Uploading recovery helper to %s", remote_recovery)
        recovery_content = recovery_local.read_text(encoding="ascii")
        up = runner.upload_text(recovery_content, remote_recovery)
        if up.returncode != 0:
            raise RuntimeError(f"Failed to upload recovery helper: {up.stderr.strip()}")

        logger.info("Uploading owner helper to %s", remote_owner)
        up = runner.upload_text(owner_local.read_text(encoding="ascii"), remote_owner)
        if up.returncode != 0:
            raise RuntimeError(f"Failed to upload owner helper: {up.stderr.strip()}")

        up = runner.upload_text(registry_local.read_text(encoding="ascii"),
                                f"{self._remote_work_dir}/ramic_registry.py")
        if up.returncode != 0:
            raise RuntimeError("Failed to upload instance registry helper")

        logger.info("Uploading IL script to %s", remote_il)
        il_content = il_local.read_text(encoding="utf-8")
        up = runner.upload_text(il_content, remote_il)
        if up.returncode != 0:
            raise RuntimeError(f"Failed to upload IL script: {up.stderr.strip()}")

        setup_content = _generate_virtuoso_setup_il(
            remote_daemon,
            remote_il,
            python_cmd,
            port=0 if self._discover_instances else self._port,
            identity_path=None if self._discover_instances else remote_identity,
            scratch_root=scratch_root if self._discover_instances else None,
            listen_all=self._listen_all,
        )
        previous = runner.run_command(f"cat {shlex.quote(remote_setup)} 2>/dev/null || true")
        self._note_replaced_setup(remote_setup, previous.stdout if previous.returncode == 0 else "")
        logger.info("Uploading setup script to %s", remote_setup)
        up = runner.upload_text(setup_content, remote_setup)
        if up.returncode != 0:
            raise RuntimeError(f"Failed to upload setup script: {up.stderr.strip()}")

        # A split deployment only works when the generated paths are visible
        # from both the CIW and the host where ipcBeginProcess launches Python.
        visibility_checks = [
            ("daemon", daemon_runner, remote_daemon),
            ("daemon recovery helper", daemon_runner, remote_recovery),
            ("daemon owner helper", daemon_runner, remote_owner),
            ("GUI", self.gui_runner, remote_setup),
        ]
        for label, check_runner, path in visibility_checks:
            if check_runner is None or check_runner is runner:
                continue
            visible = check_runner.run_command(f"test -r {shlex.quote(path)}")
            if visible.returncode != 0:
                raise RuntimeError(
                    f"Bridge deployment path {path!r} is not readable on the {label} host. "
                    "Set VB_REMOTE_SCRATCH_ROOT to a shared home or scratch directory "
                    "visible to the deployment, GUI, and daemon hosts."
                )

        self._remote_setup_done = True
        self._remote_virtuoso_setup_path = remote_setup
        self._remote_identity_path = remote_identity
        logger.info("Remote setup complete; setup script at %s (using %s)", remote_setup, python_cmd)

    def _note_replaced_setup(self, path: str, previous: str) -> None:
        # Two configurations sharing a profile deploy to the same directory;
        # a .cdsinit that loads this file follows whichever started last.
        old_port = _setup_port(previous)
        self._replaced_setup_port = old_port if old_port not in (None, self._port) else None
        if self._replaced_setup_port is not None:
            logger.warning(
                "Replacing %s, which started the daemon on port %d, with port %d",
                path, old_port, self._port,
            )

    def ensure_local_setup(self) -> None:
        """Generate virtuoso_setup.il locally (no SSH needed)."""
        if self._remote_setup_done:
            return

        if self._discover_instances:
            self._publish_generated_setup(None, sys.executable, sys.version_info[0])
            return

        python_cmd = sys.executable
        python_major = sys.version_info[0]

        daemon_local = _find_ramic_bridge_daemon(python_major)
        recovery_local = _find_ramic_helper("ramic_request_recovery.py")
        owner_local = _find_ramic_helper("ramic_owner.py")
        registry_local = _find_ramic_helper("ramic_registry.py")
        il_local = _find_ramic_bridge_il()

        # Determine local work directory
        if self._profile and not self._discover_instances:
            work_dir = state_dir() / f"local_{self._profile}"
        else:
            work_dir = state_dir() / "local"
        work_dir.mkdir(parents=True, exist_ok=True)

        # Copy daemon and IL files into the work directory
        local_daemon = work_dir / daemon_local.name
        local_recovery = work_dir / recovery_local.name
        local_owner = work_dir / owner_local.name
        local_il = work_dir / "ramic_bridge.il"
        local_setup = work_dir / "virtuoso_setup.il"
        local_identity = work_dir / "daemon_identity.txt"

        shutil.copy2(daemon_local, local_daemon)
        shutil.copy2(recovery_local, local_recovery)
        shutil.copy2(owner_local, local_owner)
        shutil.copy2(registry_local, work_dir / registry_local.name)

        il_content = il_local.read_text(encoding="utf-8")
        local_il.write_text(il_content, encoding="utf-8")

        setup_content = _generate_virtuoso_setup_il(
            str(local_daemon),
            str(local_il),
            python_cmd,
            port=0 if self._discover_instances else self._port,
            identity_path=None if self._discover_instances else str(local_identity),
            scratch_root=self._instance_root() if self._discover_instances else None,
            listen_all=self._listen_all,
        )
        self._note_replaced_setup(
            str(local_setup),
            local_setup.read_text(encoding="utf-8") if local_setup.is_file() else "",
        )
        local_setup.write_text(setup_content, encoding="utf-8")
        local_identity.unlink(missing_ok=True)

        self._remote_setup_done = True
        self._remote_virtuoso_setup_path = str(local_setup)
        self._remote_identity_path = str(local_identity)
        self._remote_work_dir = str(work_dir)
        logger.info(
            "Local setup complete; setup script at %s (using %s)",
            local_setup, python_cmd,
        )

    def allow_setup_replacement(self) -> None:
        """Authorize this operation to replace the generated account bundle."""
        if self._shared_setup:
            raise ValueError("--replace-setup cannot replace VB_DAEMON_SETUP; install a new versioned bundle")
        self._replace_setup = True

    def _publish_generated_setup(self, runner: SSHRunner | None, python: str, major: int) -> None:
        if self._shared_setup:
            raise ValueError("Shared installation is never overwritten by the client")
        root = instance_scratch_root(runner) if runner is not None else str(state_dir())
        destination = root + ("/daemon" if runner is not None else "/local")
        daemon = "ramic_bridge_daemon_3.py" if major >= 3 else "ramic_bridge_daemon_27.py"
        names = ("ramic_bridge.il", "ramic_bridge_daemon_3.py", "ramic_bridge_daemon_27.py",
                 "ramic_owner.py", "ramic_request_recovery.py", "ramic_registry.py")
        files = {name: _find_ramic_helper(name).read_text(encoding="utf-8") for name in names}
        files["virtuoso_setup.il"] = _generate_virtuoso_setup_il(
            destination + "/" + daemon, destination + "/ramic_bridge.il", python,
            port=0, scratch_root=self._instance_root(),
            listen_all=self._listen_all)
        if self._listen_all:
            print("[warning] Generated account bootstrap will listen on all interfaces for future CIWs; "
                  "authentication remains mandatory. Existing differing files require --replace-setup.", flush=True)
        if self._replace_setup:
            print(f"[setup] Explicit replacement authorized for {destination!r}; old bundle will be retained.", flush=True)
        if runner is None:
            from virtuoso_bridge.virtuoso.basic.resources.ramic_deploy import publish
            Path(root).mkdir(mode=0o700, parents=True, exist_ok=True)
            result = publish(destination, files, self._replace_setup)
        else:
            mkdir = runner.run_command(f"mkdir -p {shlex.quote(root)}", timeout=15)
            if mkdir.returncode:
                raise RuntimeError(f"Refusing to deploy into {root!r}: {mkdir.stderr.strip()}")
            payload_path = root + "/.daemon-input-" + uuid.uuid4().hex + ".json"
            payload = json.dumps(dict(destination=destination, files=files, replace=self._replace_setup))
            try:
                uploaded = runner.upload_text(payload, payload_path, timeout=30)
                if uploaded.returncode:
                    raise RuntimeError("Could not stage the intended account bundle")
                source = _find_ramic_helper("ramic_deploy.py").read_text(encoding="ascii")
                published = runner.run_command(shlex.quote(python) + " -c " + shlex.quote(source)
                                               + " " + shlex.quote(payload_path), timeout=30)
                if published.returncode:
                    raise RuntimeError(published.stderr.strip() or "Account bundle publication failed")
                result = json.loads(published.stdout)
            finally:
                runner.run_command("rm -f -- " + shlex.quote(payload_path), timeout=10)
            for role_runner in (self._require_runner(), self.gui_runner):
                if role_runner is not runner:
                    check = role_runner.run_command("test -r " + shlex.quote(destination + "/virtuoso_setup.il"), timeout=10)
                    if check.returncode:
                        raise RuntimeError("Generated setup is not visible on all configured hosts: " + destination)
        if result.get("backup"):
            from virtuoso_bridge.instance_discovery import display_text
            print("[setup] Previous bundle retained at " + display_text(result["backup"]), flush=True)
        for warning in result.get("warnings", []):
            from virtuoso_bridge.instance_discovery import display_text
            print("[setup] " + display_text(warning), flush=True)
        self._remote_work_dir = destination
        self._remote_virtuoso_setup_path = destination + "/virtuoso_setup.il"
        self._remote_identity_path = None
        self._remote_setup_done = True

    # -- SSH tunnel (delegated to SSHRunner) ----------------------------------

    def _adopt_saved_tunnel(self, local_port: int) -> bool:
        """Adopt only a live tunnel whose saved endpoints exactly match."""
        state = self.read_state(self._profile, target=self.target) or {}
        saved_host = str(state.get("daemon_host") or state.get("remote_host") or "")
        expected_host = self._daemon_host.strip().rstrip(".").lower()
        saved_remote_port = state.get("remote_port")
        try:
            endpoints_match = (
                state.get("mode") == "remote"
                and int(state.get("port")) == local_port
                and int(saved_remote_port) == self._port
                and saved_host.strip().rstrip(".").lower() == expected_host
            )
        except (TypeError, ValueError):
            endpoints_match = False
        pid = state.get("tunnel_pid")
        if not endpoints_match or not _pid_is_alive(pid):
            return False
        if not SSHRunner.can_reach_port(local_port):
            return False
        self._require_runner().tunnel_pid = int(pid)
        self._tunnel_endpoint = (local_port, self._port, int(pid))
        return True

    def _active_tunnel_matches(self) -> bool:
        runner = self._require_runner()
        return runner.is_tunnel_alive and self._tunnel_endpoint == (
            self._local_port, self._port, runner.tunnel_pid)

    def _select_local_port(self, local_port: int) -> None:
        if local_port == self._local_port:
            return
        logger.info("Local port %d was busy, using port %d", self._local_port, local_port)
        print(f"[port] {self._local_port} busy, auto-switched to {local_port}", flush=True)
        self._local_port = local_port
        _update_env_file(_profiled_env_key("VB_LOCAL_PORT", self._profile), str(local_port))

    def ensure_tunnel(self) -> None:
        """Ensure SSH tunnel is running, auto-retry on port conflict."""
        if _is_localhost(self._remote_host):
            return
        relevant_paths = {_state_file(self._profile, self.target), _selection_file(self._profile),
                          *_legacy_state_files(self._profile)}
        unsafe = [item for item in self.inspect_states(self._profile)
                  if item.get("error") and Path(item["path"]) in relevant_paths]
        if unsafe:
            raise RuntimeError("Cannot reconcile retained tunnel state at %r; use status --inspect-state "
                               "before starting another forward" % unsafe[0]["path"])
        runner = self._require_runner()
        if runner.is_tunnel_alive:
            if self._active_tunnel_matches():
                return
            # Leave the old forward alive for its users, but do not reuse or
            # stop it after this client selects another daemon.
            runner.detach_port_forward()
            self._tunnel_endpoint = None

        max_attempts = 10
        local_port = self._local_port
        for attempt in range(max_attempts):
            if SSHRunner.can_reach_port(local_port):
                if self._adopt_saved_tunnel(local_port):
                    self._select_local_port(local_port)
                    return
                logger.info("Local port %d is held by an unknown listener", local_port)
                local_port += 1
                continue
            settle = _TUNNEL_STARTUP_SETTLE_SECONDS
            if self._jump_host:
                settle = max(settle, 3.0)
            proc = runner.start_port_forward(local_port, settle=settle, remote_port=self._port)
            if proc is None:
                if self._adopt_saved_tunnel(local_port):
                    self._select_local_port(local_port)
                    return
                logger.info("Local port %d became occupied by an unknown listener", local_port)
                local_port += 1
                continue
            if proc.poll() is None:
                # Tunnel running
                self._select_local_port(local_port)
                self._tunnel_endpoint = (local_port, self._port, runner.tunnel_pid)
                logger.info(
                    "SSH tunnel established (PID %d): localhost:%d -> %s:localhost:%d",
                    proc.pid, local_port, self._remote_host, self._port,
                )
                return
            # Failed
            stderr = proc.stderr
            err_msg = stderr.read().decode("utf-8", errors="ignore") if stderr else ""
            if "address already in use" in err_msg.lower() or "cannot listen" in err_msg.lower():
                logger.info("Local port %d in use, trying %d", local_port, local_port + 1)
                local_port += 1
                continue
            raise RuntimeError(f"SSH tunnel failed: {err_msg.strip()}")

        raise RuntimeError(f"No free local port found after {max_attempts} attempts ({self._local_port}-{local_port - 1})")

    # -- high-level lifecycle -----------------------------------------------

    def _instance_root(self) -> str:
        from virtuoso_bridge.instance_discovery import DiscoveryError
        if self._ssh_runner is not None:
            return instance_scratch_root(self._ssh_runner)
        if not sys.platform.startswith("linux") or not Path("/proc/self/fd").is_dir():
            raise DiscoveryError("Local instance discovery requires Linux with /proc/self/fd; "
                                 "set VB_REMOTE_HOST to the Linux daemon host. Spectre is independent.")
        from virtuoso_bridge.virtuoso.basic.resources.ramic_registry import expand_root
        return expand_root(instance_scratch_root())

    def list_instances(self, *, timeout: float | None = None) -> list[dict]:
        """Read live records without changing allocation, credentials or selection."""
        from virtuoso_bridge.instance_discovery import DiscoveryError, validate_record
        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        root = self._instance_root()
        if self._ssh_runner is None:
            from virtuoso_bridge.virtuoso.basic.resources.ramic_registry import discover
            try:
                rows = discover(root=root, read_only=True)
            except (OSError, ValueError, RuntimeError) as exc:
                raise DiscoveryError("Cannot read instance registry at %r: %s" % (root, exc)) from exc
        else:
            if deadline <= time.monotonic():
                raise TimeoutError("Instance discovery deadline expired")
            python, _, _ = self._detect_remote_python(self._ssh_runner, timeout=deadline - time.monotonic())
            source = _find_ramic_helper("ramic_registry.py").read_text(encoding="ascii")
            args = ["--scratch-root", root, "--read-only"]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Instance discovery deadline expired")
            result = self._ssh_runner.run_command(
                shlex.quote(python) + " -c " + shlex.quote(source) + " " + shlex.join(args),
                timeout=remaining)
            if result.returncode:
                raise DiscoveryError("Cannot read instance registry over SSH at %r: %s" % (root, result.stderr.strip()))
            try:
                rows = json.loads(result.stdout)
                if not isinstance(rows, list):
                    raise ValueError("expected instance list")
            except ValueError as exc:
                raise DiscoveryError("Invalid instance registry response") from exc
        try:
            for row in rows:
                validate_record(row)
        except ValueError as exc:
            raise DiscoveryError("Invalid instance registry response: " + str(exc)) from exc
        return rows

    def discover_instance(self, *, provision: bool = True) -> None:
        from virtuoso_bridge.instance_discovery import InstanceSelectionError, validate_selector
        validate_selector(self._expected_work_dir, self._selected_port)
        rows = self.list_instances()
        record = self.select_record(rows)
        if record is None:
            if self.use_legacy_endpoint(rows):
                return
            if not provision:
                raise InstanceSelectionError("No valid Virtuoso instance found; run virtuoso-bridge start for setup instructions")
            if self._shared_setup:
                self._remote_virtuoso_setup_path = self._shared_setup
            elif self._ssh_runner is None:
                self.ensure_local_setup()
            else:
                self.ensure_remote_setup()
            raise InstanceSelectionError(
                'No valid Virtuoso instance found under %r. Check the daemon RB_SCRATCH_ROOT / '
                'VB_REMOTE_SCRATCH_ROOT and client root agree.\n'
                'Run virtuoso-bridge restart, or load("%s") in the intended CIW. '
                'Add that load line to .cdsinit for future sessions.\n'
                'Then run virtuoso-bridge start again.' % (self._instance_root(), self.setup_path))
        self.instance_record = record
        self._legacy_fallback = False
        self._port = record["port"]
        # A loaded instance needs no deployment, regeneration, or CIW reload.
        self._remote_setup_done = True
        self._remote_virtuoso_setup_path = self._shared_setup or None
        self._daemon_token = self.refresh_daemon_token()

    def use_legacy_endpoint(self, rows: list[dict]) -> bool:
        """Use an old configured endpoint only after empty, successful discovery.

        A saved automatic identity cannot silently downgrade to a blind endpoint.
        This does not provision files or change the loaded CIW's configuration.
        """
        import posixpath
        candidates = [row for row in rows if not self._expected_work_dir
                      or posixpath.normpath(row["work_dir"]) == posixpath.normpath(self._expected_work_dir)]
        if (candidates or self.instance_record or self._selected_port is not None or self._legacy_port is None
                or any(row["port"] == self._legacy_port for row in rows)):
            return False
        self._legacy_fallback = True
        self._port = self._legacy_port
        self._remote_setup_done = True
        state = self.read_state(self._profile, target=self.target) or {}
        self._remote_virtuoso_setup_path = state.get("setup_path")
        self._remote_identity_path = state.get("identity_path")
        self._daemon_token = None
        return True

    def warm(self, timeout: int = 15) -> None:
        """Full startup: discover, authenticate, and forward the selected instance."""
        if self._discover_instances:
            self.discover_instance()
        if _is_localhost(self._remote_host):
            self.ensure_local_setup()
            self._daemon_endpoint_hostname = socket.gethostname()
            self._daemon_token = self.ensure_daemon_token()
            self._verify_discovered_instance(timeout)
            self.save_state()
            return
        try:
            self.ensure_remote_setup()
            self._daemon_token = self.ensure_daemon_token()
            runners = {id(runner): runner for runner in self._role_runners.values()}
            for runner in runners.values():
                if runner.persistent_shell_enabled:
                    runner.ensure_persistent_shell(timeout=timeout)
            self._daemon_endpoint_hostname = self.probe_daemon_endpoint_hostname()
            self.ensure_tunnel()
            self._verify_discovered_instance(timeout)
            self.save_state()
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def reconcile_instance(self, timeout: int = 15) -> bool:
        """Reconcile a vanished saved daemon before application transmission.

        A fresh client may follow a unique replacement only inside its explicit
        project constraint. Authentication failures never invoke this operation.
        """
        from virtuoso_bridge.instance_discovery import InstanceSelectionError, select_instance, validate_record
        validate_record(self.instance_record)
        rows = self.list_instances()
        if self.instance_record in rows:
            select_instance([self.instance_record], self._expected_work_dir, self._selected_port)
            return False
        if not self._expected_work_dir:
            raise InstanceSelectionError("Saved instance is no longer live. Use virtuoso-bridge sessions "
                                         "and virtuoso-bridge start --port PORT to select explicitly.")
        replacement = select_instance(rows, self._expected_work_dir, self._selected_port)
        if replacement is None:
            raise InstanceSelectionError("No replacement in the configured project; run virtuoso-bridge start")
        self.connect_instance(replacement, timeout)
        return True

    def select_record(self, rows: list[dict]) -> dict | None:
        """Prefer a still-live saved choice while respecting exact selectors."""
        from virtuoso_bridge.instance_discovery import select_instance
        saved = self.instance_record
        if saved in rows and (self._selected_port is None or saved["port"] == self._selected_port):
            match = select_instance([saved], self._expected_work_dir)
            if match:
                return match
        if self._selected_port is None and self._legacy_port is not None:
            preferred = [row for row in rows if row["port"] == self._legacy_port]
            if preferred:
                match = select_instance(preferred, self._expected_work_dir)
                if match:
                    return match
        return select_instance(rows, self._expected_work_dir, self._selected_port)

    def connect_instance(self, record: dict, timeout: int = 15, *,
                         setup_path: str | None = None, publish: bool = True) -> None:
        """Authenticate an explicit record before publishing connection state."""
        from virtuoso_bridge.instance_discovery import validate_record, select_instance
        validate_record(record)
        select_instance([record], self._expected_work_dir, self._selected_port)
        self.instance_record = record
        self._legacy_fallback = False
        self._port = record["port"]
        self._remote_setup_done = True
        if setup_path is not None:
            self._remote_virtuoso_setup_path = setup_path
        self._daemon_token = self.read_daemon_token()
        if not self._daemon_token:
            raise daemon_auth.DaemonTokenError("Cannot read the private daemon credential")
        if self._ssh_runner is not None:
            self.ensure_tunnel()
        self.verify_instance(timeout)
        if publish:
            self.save_state()

    def ciw_process_identity(self, record: dict, *, timeout: float = 10) -> str:
        """Read the selected CIW's Linux birth identity without sending SKILL."""
        from virtuoso_bridge.instance_discovery import validate_record
        validate_record(record)
        source = _find_ramic_helper("ramic_owner.py").read_text(encoding="ascii")
        source += ("\nimport json\npid = %d\n"
                   "identity = _identity(pid)\n"
                   "assert identity and os.stat('/proc/%%d' %% pid).st_uid == %d\n"
                   "assert os.readlink('/proc/%%d/cwd' %% pid) == json.loads(%r)\n"
                   "print(json.dumps([_boot_id(), identity]))\n"
                   % (record["pid"], record["uid"], json.dumps(record["work_dir"], ensure_ascii=True)))
        if self._ssh_runner is not None:
            deadline = time.monotonic() + timeout
            python, _, _ = self._detect_remote_python(self._ssh_runner, timeout=timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CIW identity deadline expired")
            result = self._ssh_runner.run_command(shlex.quote(python) + " -c " + shlex.quote(source), timeout=remaining)
            if result.returncode:
                raise RuntimeError("Cannot verify the original CIW process birth identity; restart refused")
            value = json.loads(result.stdout)
        else:
            from virtuoso_bridge.virtuoso.basic.resources.ramic_owner import _boot_id, _identity
            proc = Path("/proc") / str(record["pid"])
            if proc.stat().st_uid != record["uid"] or os.readlink(proc / "cwd") != record["work_dir"]:
                raise RuntimeError("CIW ownership/directory differs from the selected record")
            value = [_boot_id(), _identity(record["pid"])]
        if not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, str) and v for v in value):
            raise RuntimeError("Cannot verify the original CIW process birth identity")
        return ":".join(value)

    def restart_instance(self, timeout: int = 30) -> None:
        """Restart the authenticated saved CIW once, then follow only its child."""
        from virtuoso_bridge.instance_discovery import InstanceSelectionError, validate_record
        from virtuoso_bridge.models import ExecutionStatus
        from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
        if self.instance_record is None:
            self.discover_instance(provision=False)
            if self._legacy_fallback:
                self._restart_legacy_instance(timeout)
                return
        original = dict(validate_record(self.instance_record))
        if original not in self.list_instances():
            raise InstanceSelectionError("Saved instance is no longer live; select explicitly before restart")
        birth = self.ciw_process_identity(original)
        self.connect_instance(original, min(timeout, 10), publish=False)
        client = VirtuosoClient(host="127.0.0.1", port=self.port, tunnel=self,
                                daemon_token=self._daemon_token, timeout=5, log_to_ciw=False)
        client.use_current_selection()
        caps = client._ensure_daemon_capabilities(time.monotonic() + 5)
        if caps.get("lane", {}).get("state", "idle") != "idle":
            raise RuntimeError("Selected CIW bridge lane is busy; restart refused")
        if self.ciw_process_identity(original) != birth:
            raise RuntimeError("Original CIW identity changed; restart refused")
        deadline = time.monotonic() + timeout
        # The reply precedes the timer. A dropped reply never permits replay.
        result = client.execute_skill('hiRegTimer("RBSetPort(RBConfiguredPort)" 2)', timeout=5,
                                      retry_connect=False)
        details = "; ".join(result.errors) or result.output or "restart outcome unknown"
        delay = 0.2
        while time.monotonic() < deadline:
            if self.ciw_process_identity(original, timeout=deadline - time.monotonic()) != birth:
                raise RuntimeError("Original CIW exited or its PID was reused; retained restart evidence")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            rows = [row for row in self.list_instances(timeout=remaining)
                    if all(row[key] == original[key] for key in ("pid", "uid", "user", "work_dir"))
                    and row["instance"] != original["instance"]]
            if len(rows) > 1:
                raise RuntimeError("Multiple replacement children for the original CIW; restart unverified")
            if rows:
                if self._selected_port is not None and rows[0]["port"] != self._selected_port:
                    raise InstanceSelectionError("Restart changed the endpoint but the exact --port "
                                                 "selector no longer matches; inspect sessions and select explicitly")
                runner = self._ssh_runner
                retired = runner is None
                if runner is not None and self._active_tunnel_matches():
                    runner.stop_port_forward()
                    retired = True
                self.connect_instance(rows[0], max(1, int(deadline - time.monotonic())))
                if retired and rows[0]["port"] != original["port"]:
                    old_target = _tunnel_target(self._remote_user, self._daemon_host, original["port"])
                    path = _state_file(self._profile, old_target)
                    try:
                        state = _validate_state(_read_private_json(path))
                        if state.get("instance_record") == original:
                            path.unlink()
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError):
                        logger.warning("Restart completed; inspect retained old state at %s", path)
                return
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
            delay = min(1.5, delay * 1.5)
        raise RuntimeError("Restart was requested once but replacement was not authenticated before timeout; "
                           "retain the saved selection and inspect sessions before any retry. "
                           + (details if result.status != ExecutionStatus.SUCCESS else ""))

    def _legacy_ciw_birth(self, pid: int, timeout: float) -> list:
        """Bind an authenticated legacy PID to its actual owner and birth."""
        if type(pid) is not int or pid <= 0:
            raise RuntimeError("Legacy daemon did not report a usable authenticated CIW PID")
        source = _find_ramic_helper("ramic_owner.py").read_text(encoding="ascii")
        source += ("\nimport json\npid = %d\nidentity = _identity(pid)\n"
                   "assert identity and os.stat('/proc/%%d' %% pid).st_uid == os.geteuid()\n"
                   "print(json.dumps([_boot_id(), identity]))\n" % pid)
        if self._ssh_runner is None:
            from virtuoso_bridge.virtuoso.basic.resources.ramic_owner import _boot_id, _identity
            if Path("/proc", str(pid)).stat().st_uid != os.geteuid():
                raise RuntimeError("Legacy CIW belongs to another account")
            result = [_boot_id(), _identity(pid)]
        else:
            deadline = time.monotonic() + timeout
            python, _, _ = self._detect_remote_python(self._ssh_runner, timeout=timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Legacy CIW identity deadline expired")
            reply = self._ssh_runner.run_command(shlex.quote(python) + " -c " + shlex.quote(source), timeout=remaining)
            if reply.returncode:
                raise RuntimeError("Cannot establish legacy CIW process identity; restart refused")
            result = json.loads(reply.stdout)
        if not isinstance(result, list) or len(result) != 2 or not all(result):
            raise RuntimeError("Cannot establish legacy CIW process identity")
        return result

    def _restart_legacy_instance(self, timeout: float) -> None:
        """Use only the old loader's RBStop/RBStart, with a bounded exit wait."""
        from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
        deadline = time.monotonic() + timeout

        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError("Legacy restart outcome unconfirmed; retain state and do not replay the request")
            return budget

        self.ensure_tunnel()
        client = VirtuosoClient(port=self.port, tunnel=self, daemon_token=self.read_daemon_token(), log_to_ciw=False)
        client.use_current_selection()
        caps = client._ensure_daemon_capabilities(deadline)
        if caps.get("auth") != "on" or not caps.get("daemon_instance"):
            raise RuntimeError("Legacy restart requires authenticated daemon identity")
        if caps.get("lane", {}).get("state", "idle") != "idle":
            raise RuntimeError("Selected CIW bridge lane is busy; restart refused")
        pid = caps.get("virtuoso_pid")
        birth = self._legacy_ciw_birth(pid, remaining())
        # Same primitives as RBApplyPort, but no new-loader symbol is assumed.
        # A unique function avoids replacing another pending timer's callback.
        callback = "VBCompatRestart" + uuid.uuid4().hex
        script = (
            'procedure(%s(n) if(boundp(\'RBIpc) && RBIpc && RBIpc != \'unbound && ipcIsAliveProcess(RBIpc) then '
            'when(n > 0 hiRegTimer(sprintf(nil "%s(%%d)" n-1) 1)) '
            'else RBIpc = \'unbound RBStart())) '
            'hiRegTimer("RBStop() %s(50)" 2)' % (callback, callback, callback)
        )
        client.execute_skill(script, timeout=min(5, remaining()), retry_connect=False)
        delay = 0.2
        while remaining() > 0:
            if self._legacy_ciw_birth(pid, remaining()) != birth:
                raise RuntimeError("Original legacy CIW exited or its PID was reused; retained restart evidence")
            try:
                replacement = client._ensure_daemon_capabilities(deadline, refresh=True)
            except (ConnectionError, TimeoutError):
                replacement = None
            if replacement and replacement.get("daemon_instance") != caps["daemon_instance"]:
                if replacement.get("auth") != "on" or replacement.get("virtuoso_pid") != pid:
                    raise RuntimeError("Replacement does not match the original legacy CIW")
                self.save_state()
                return
            time.sleep(min(delay, remaining()))
            delay = min(1.5, delay * 1.5)

    def existing_setup_path(self) -> str | None:
        """Resolve an explicitly configured or already-generated bootstrap."""
        if self._shared_setup:
            return self._shared_setup
        state = self.read_state(self._profile)
        if state and state.get("setup_path"):
            return str(state["setup_path"])
        if self._ssh_runner is None:
            candidate = state_dir() / "local" / "virtuoso_setup.il"
            return str(candidate) if candidate.is_file() else None
        runner = self._require_deployment_runner()
        candidate = instance_scratch_root(runner) + "/daemon/virtuoso_setup.il"
        result = runner.run_command("test -f " + shlex.quote(candidate), timeout=10)
        return candidate if result.returncode == 0 else None

    def _verify_discovered_instance(self, timeout: int) -> None:
        if not self.instance_record and not self._legacy_fallback:
            return
        from virtuoso_bridge.instance_discovery import InstanceConnectionError
        from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
        client = VirtuosoClient(host="127.0.0.1", port=self.port, tunnel=self,
                                daemon_token=self._daemon_token, timeout=timeout)
        client.use_current_selection()
        try:
            caps = client._ensure_daemon_capabilities(time.monotonic() + timeout)
            if self._legacy_fallback and caps.get("instance_record"):
                raise RuntimeError("Endpoint advertises a registered instance absent from discovery; inspect the registry root")
            from virtuoso_bridge.instance_discovery import compatibility_message
            self.daemon_compatibility = compatibility_message(caps)
        except Exception as exc:
            if self._legacy_fallback and isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
                # A genuinely unavailable old endpoint may bootstrap a new
                # registered session. Authentication/registry errors never do.
                runner = self._ssh_runner
                if runner is not None and getattr(runner, "_tunnel_proc", None) is not None:
                    runner.stop_port_forward()
                self._legacy_fallback = False
                self._remote_setup_done = False
                if self._shared_setup:
                    self._remote_virtuoso_setup_path = self._shared_setup
                elif runner is None:
                    self.ensure_local_setup()
                else:
                    self.ensure_remote_setup()
                from virtuoso_bridge.instance_discovery import InstanceSelectionError
                raise InstanceSelectionError(
                    'Configured fixed-port endpoint is unavailable. In the intended idle CIW, '
                    'load("%s"), then run start again to discover its new session.' % self.setup_path
                ) from exc
            raise InstanceConnectionError(
                f"Daemon handshake failed at local port {self.port} "
                f"(remote port {self.remote_port}): {exc}"
            ) from exc

    def verify_instance(self, timeout: int = 15) -> None:
        """Verify the current record without discovery or state publication."""
        if self._discover_instances and not self._daemon_token:
            self._daemon_token = self.read_daemon_token()
            if not self._daemon_token:
                raise daemon_auth.DaemonTokenError("Cannot read the private daemon credential")
        self._verify_discovered_instance(timeout)

    def stop(self) -> None:
        """Kill the tunnel and clean up."""
        if _is_localhost(self._remote_host):
            # Local mode: no tunnel or SSH to tear down — just clear state
            self._clear_state()
            return

        runner = self._require_runner()
        if runner.is_tunnel_alive and not self._active_tunnel_matches():
            runner.detach_port_forward()
            self._tunnel_endpoint = None
        # Try state file PID first (may be from a previous session)
        if not runner.is_tunnel_alive:
            state = self.read_state(self._profile, target=self.target)
            if state:
                pid = state.get("tunnel_pid")
                if pid:
                    runner.tunnel_pid = pid

        try:
            runner.stop_port_forward()

            if (not self._discover_instances and not self._keep_remote_files
                    and self._remote_setup_done and self._remote_work_dir):
                try:
                    deployment_runner = self._require_deployment_runner()
                    deployment_runner.run_command(f"rm -rf {shlex.quote(self._remote_work_dir)}")
                except Exception:
                    logger.warning("Failed to clean up remote files at %s", self._remote_work_dir)
        except Exception as exc:
            raise RuntimeError(
                f"{exc}. Retained state: {_state_file(self._profile, self.target)}. "
                "Run virtuoso-bridge status --inspect-state using the original --env/profile; "
                "see the connection guide's legacy tunnel recovery procedure."
            ) from exc
        finally:
            self.close()
        self._clear_state()

    def close(self) -> None:
        """Close SSH runner without killing the tunnel (it survives for other scripts)."""
        closed: set[int] = set()
        for runner in self._role_runners.values():
            if id(runner) in closed:
                continue
            closed.add(id(runner))
            try:
                runner.close()
            except Exception:
                pass

    def probe_daemon_endpoint_hostname(self) -> str:
        """Return the OS hostname reached through the configured daemon SSH endpoint."""
        if _is_localhost(self._daemon_host):
            return socket.gethostname()
        result = self._require_runner().run_command(
            "hostname -f 2>/dev/null || hostname 2>/dev/null || true",
            timeout=min(self._timeout, 10),
        )
        return next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")

    def read_daemon_identity(self) -> dict[str, str]:
        """Read the banner identity written by the CIW-side loader.

        This path remains usable when the TCP tunnel targets the wrong host,
        which is precisely when a protocol-level identity query cannot work.
        """
        identity_path = self._remote_identity_path
        if not identity_path:
            state = self.read_state(self._profile, target=self.target) or {}
            identity_path = str(state.get("identity_path") or "")
        if not identity_path:
            return {}
        if _is_localhost(self._deploy_host):
            try:
                text = Path(identity_path).read_text(encoding="utf-8")
            except OSError:
                return {}
        else:
            result = self._require_deployment_runner().run_command(
                f"test -r {shlex.quote(identity_path)} && cat {shlex.quote(identity_path)}",
                timeout=min(self._timeout, 10),
            )
            if result.returncode != 0:
                return {}
            text = result.stdout
        identity: dict[str, str] = {}
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"host", "ip", "pid", "bind"}:
                identity[key] = value.strip()
        return identity

    # -- state file ---------------------------------------------------------

    @property
    def target(self) -> str | None:
        """``user@host:port`` of the daemon this client's tunnel leads to."""
        return _tunnel_target(self._remote_user, self._daemon_host, self._port)

    def _clear_state(self) -> None:
        """Remove this target's state, and an old-format file describing it."""
        for path in [_state_file(self._profile, self.target), _selection_file(self._profile),
                     *_legacy_state_files(self._profile)]:
            try:
                state = _read_private_json(path, legacy_metadata=path != _selection_file(self._profile))
                if path == _selection_file(self._profile):
                    if not isinstance(state, dict):
                        raise ValueError("Invalid saved selection")
                    from virtuoso_bridge.instance_discovery import validate_record
                    validate_record(state.get("record"))
                    if state.get("target") != self.target:
                        continue
                else:
                    _validate_state(state)
                    if not self.target or not _state_target_matches(state, self.target):
                        continue
                path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                logger.warning("Tunnel stopped; could not safely clear state at %s; inspect this file", path)
                continue

    def save_state(self) -> None:
        """Save tunnel state so other processes can find the port."""
        state_dir().mkdir(parents=True, exist_ok=True)
        is_local = _is_localhost(self._daemon_host)
        tunnel_pid = None
        if not is_local:
            tunnel_pid = self._require_runner().tunnel_pid
        state = {
            "mode": "local" if is_local else "remote",
            "port": self._port if is_local else self._local_port,
            "remote_port": self._port,
            "tunnel_pid": tunnel_pid,
            "target": self.target,
            "remote_user": self._remote_user,
            "remote_host": self._remote_host,
            "daemon_host": self._daemon_host,
            "deploy_host": self._deploy_host,
            "gui_host": self._gui_host,
            "spectre_host": self._spectre_host,
            "daemon_endpoint_hostname": self._daemon_endpoint_hostname,
            "setup_path": self._remote_virtuoso_setup_path,
            "identity_path": self._remote_identity_path,
            "profile": self._profile,
            "started_at": time.time(),
        }
        # Credentials never enter connection metadata. Keep the old complete
        # state until atomic replacement succeeds.
        if self._discover_instances and self.instance_record:
            from virtuoso_bridge.instance_discovery import validate_record
            validate_record(self.instance_record)
            if self.instance_record["port"] != self._port:
                raise ValueError("Instance record and tunnel ports differ")
            state["instance_record"] = self.instance_record
        _write_private_json(_state_file(self._profile, self.target), state)
        for legacy in _legacy_state_files(self._profile):
            try:
                previous = _validate_state(_read_private_json(legacy, legacy_metadata=True))
            except (OSError, ValueError):
                continue
            if isinstance(previous, dict) and _state_target_matches(previous, self.target):
                legacy.unlink(missing_ok=True)
        if self._discover_instances and self.instance_record:
            selection = {"target": self.target, "port": self._port, "record": self.instance_record}
            _write_private_json(_selection_file(self._profile), selection)

    @staticmethod
    def read_state(profile: str | None = None, *, target: Any = _CONFIGURED) -> dict[str, Any] | None:
        """Read the saved state of the tunnel to *target*.

        *target* defaults to the configuration loaded into the environment,
        so a tunnel another ``.env`` started for a different host is not
        mistaken for this one. ``None`` ignores the configured target;
        remote state must still record its destination port.
        """
        if target is _CONFIGURED:
            target = _read_selection(profile).get("target")
            suffix = f"_{profile}" if profile else ""
            if not target and os.getenv(f"VB_REMOTE_WORK_DIR{suffix}", "").strip():
                return None  # legacy state has no proof of the requested directory
            roles = remote_host_roles_from_os(profile, load=False)
            if (not target and roles.daemon_host and not _is_localhost(roles.daemon_host)
                    and not os.getenv(f"VB_REMOTE_PORT{suffix}", "").strip()):
                return None  # automatic mode needs a saved discovery selection
            target = target or _configured_target(profile)
        if target:
            candidates = [_state_file(profile, target)]
        else:
            # Without a configured target, a single saved tunnel is unambiguous.
            prefix = _state_file(profile).stem + "-"
            saved = [sf for sf in state_dir().glob(f"{prefix}*.json")]
            candidates = saved if len(saved) == 1 else []
        candidates += _legacy_state_files(profile)
        for sf in candidates:
            try:
                state = _validate_state(_read_private_json(sf, legacy_metadata=True))
            except (OSError, ValueError):
                continue
            if target and not _state_target_matches(state, target):
                continue
            if state.get("mode") == "remote" and not state.get("remote_port"):
                continue
            return state
        return None

    @classmethod
    def inspect_states(cls, profile: str | None = None) -> list[dict[str, Any]]:
        """Inspect retained local files without discovery, credentials or SSH."""
        paths = set(state_dir().glob("state*.json")) | set(_legacy_state_files(profile))
        paths.add(_selection_file(profile))
        results = []
        for path in sorted(paths):
            try:
                value = _read_private_json(path, legacy_metadata=not path.name.startswith("instance-"))
                if path.name.startswith("instance-"):
                    from virtuoso_bridge.instance_discovery import validate_record
                    record = validate_record(value.get("record"))
                    result = {"path": str(path), "selection_port": record["port"],
                              "target": value.get("target")}
                else:
                    value = _validate_state(value)
                    result = {"path": str(path), **{key: value.get(key) for key in (
                        "target", "profile", "port", "remote_port", "tunnel_pid")},
                        "birth_identity_saved": bool(value.get("tunnel_identity"))}
                results.append(result)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, AttributeError) as exc:
                results.append({"path": str(path), "error": str(exc)})
        return results

    @classmethod
    def running_targets(cls) -> list[tuple[str | None, str]]:
        """(profile, target) of every live tunnel whose state names a target."""
        found = []
        for sf in sorted(state_dir().glob("state*.json")):
            try:
                state = _validate_state(_read_private_json(sf, legacy_metadata=True))
            except (OSError, ValueError):
                continue
            if isinstance(state, dict) and state.get("target") and cls._state_is_live(state):
                found.append((state.get("profile"), str(state["target"])))
        return found

    @classmethod
    def is_running(cls, profile: str | None = None) -> bool:
        """Check if a saved, managed tunnel is alive and reachable.

        For local mode, the state file existing is sufficient — the daemon
        may not be loaded in CIW yet, so we skip port checks.
        """
        return cls._state_is_live(cls.read_state(profile))

    @staticmethod
    def _state_is_live(state: dict[str, Any] | None) -> bool:
        if not state:
            return False
        if state.get("mode") == "local":
            return True
        if state.get("mode") != "remote":
            return False
        port = state.get("port")
        remote_port = state.get("remote_port")
        pid = state.get("tunnel_pid")
        if any(type(value) is not int for value in (port, remote_port, pid)) or pid <= 0:
            return False
        try:
            local_port = int(port)
        except (TypeError, ValueError):
            return False
        try:
            if not 1 <= int(remote_port) <= 65535:
                return False
        except (TypeError, ValueError):
            return False
        if state.get("target") and not _state_target_matches(state, state["target"]):
            return False
        if not 1 <= local_port <= 65535 or not _pid_is_alive(pid):
            return False
        return SSHRunner.can_reach_port(local_port)

    # -- file transfer (delegated to SSHRunner) -----------------------------

    def upload_file(self, local_path: Path, remote_path: str, timeout: int | None = None) -> CommandResult:
        runner = self._require_deployment_runner()
        return runner.upload(local_path, remote_path, timeout=timeout or self._timeout)

    def download_file(self, remote_path: str, local_path: Path, timeout: int | None = None, recursive: bool = False) -> CommandResult:
        runner = self._require_deployment_runner()
        return runner.download(remote_path, local_path, recursive=recursive, timeout=timeout or self._timeout)

    def upload_text(self, text: str, remote_path: str, timeout: int | None = None) -> CommandResult:
        runner = self._require_deployment_runner()
        return runner.upload_text(text, remote_path, timeout=timeout or self._timeout)

    def run_command(self, cmd: str, timeout: int | None = None) -> CommandResult:
        runner = self._require_deployment_runner()
        return runner.run_command(cmd, timeout=timeout or self._timeout)

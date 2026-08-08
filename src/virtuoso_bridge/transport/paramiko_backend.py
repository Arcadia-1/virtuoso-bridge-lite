"""Persistent Paramiko SSH transport with bounded session multiplexing."""

from __future__ import annotations

import errno
import getpass
import hashlib
import logging
import os
import queue
import re
import shlex
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit

from virtuoso_bridge.transport.transfer import (
    FileDownloadPlan,
    TarDownloadPlan,
    TarUploadPlan,
    TextUploadPlan,
    discard_stage,
    install_staged_item,
    install_staged_path,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Deadline:
    timeout: float
    deadline: float

    @classmethod
    def start(cls, timeout: float) -> "_Deadline":
        return cls(timeout=float(timeout), deadline=time.monotonic() + float(timeout))

    def remaining(self, command: object) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0.0:
            raise subprocess.TimeoutExpired(command, self.timeout)
        return remaining


@dataclass(frozen=True)
class _Endpoint:
    host_alias: str
    hostname: str
    username: str | None
    port: int
    key_filenames: tuple[str, ...]
    identities_only: bool = False
    host_key_alias: str | None = None
    known_hosts_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _ProxyJump:
    host: str
    username: str | None
    port: int | None


_SSH_PATH_TOKEN_RE = re.compile(r"%([%CdhikLlnpru])")
_PROXY_TOKEN_RE = re.compile(r"%([%hnpr])")


def _default_known_hosts_files(
    *,
    home: Path | None = None,
    platform_name: str | None = None,
    program_data: str | None = None,
) -> tuple[Path, ...]:
    user_home = Path.home() if home is None else home
    platform = os.name if platform_name is None else platform_name
    paths = [
        user_home / ".ssh" / "known_hosts",
        user_home / ".ssh" / "known_hosts2",
    ]
    if platform == "nt":
        windows_program_data = (
            os.environ.get("PROGRAMDATA") if program_data is None else program_data
        )
        if windows_program_data:
            paths.extend(
                [
                    Path(windows_program_data) / "ssh" / "ssh_known_hosts",
                    Path(windows_program_data) / "ssh" / "ssh_known_hosts2",
                ]
            )
    else:
        paths.extend(
            [
                Path("/etc/ssh/ssh_known_hosts"),
                Path("/etc/ssh/ssh_known_hosts2"),
            ]
        )
    return tuple(paths)


def _expand_ssh_path(
    value: str,
    *,
    host_alias: str,
    hostname: str,
    host_key_alias: str,
    port: int,
    remote_user: str | None,
    proxy_jump: str,
) -> Path:
    local_user = getpass.getuser()
    remote = remote_user or local_user
    local_fqdn = socket.getfqdn()
    connection_hash = hashlib.sha1(
        f"{local_fqdn}{hostname}{port}{remote}{proxy_jump}".encode("utf-8")
    ).hexdigest()
    tokens = {
        "%": "%",
        "C": connection_hash,
        "d": str(Path.home()),
        "h": hostname,
        "i": str(os.getuid()) if hasattr(os, "getuid") else local_user,
        "k": host_key_alias,
        "L": socket.gethostname().split(".", 1)[0],
        "l": local_fqdn,
        "n": host_alias,
        "p": str(port),
        "r": remote,
        "u": local_user,
    }

    def replace_token(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in tokens:
            raise ValueError(f"Unsupported SSH path token %{token}")
        return tokens[token]

    expanded = _SSH_PATH_TOKEN_RE.sub(replace_token, value)
    if os.name == "nt" and expanded.startswith("__PROGRAMDATA__"):
        program_data = os.environ.get("PROGRAMDATA")
        if program_data:
            expanded = program_data + expanded[len("__PROGRAMDATA__") :]
    return Path(os.path.expandvars(os.path.expanduser(expanded)))


def _windows_no_window_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    startupinfo.wShowWindow = 0
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        "close_fds": True,
        "startupinfo": startupinfo,
    }


def _read_stream(
    stream: Any,
    chunks: list[bytes],
    failures: "queue.Queue[BaseException]",
) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            chunks.append(chunk)
    except BaseException as exc:  # noqa: BLE001
        failures.put(exc)


def _send_stream_to_channel(
    stream: Any,
    channel: Any,
    failures: "queue.Queue[BaseException]",
) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            channel.sendall(chunk)
        channel.shutdown_write()
    except BaseException as exc:  # noqa: BLE001
        failures.put(exc)


def _copy_stream(
    source: Any,
    destination: Any,
    failures: "queue.Queue[BaseException]",
) -> None:
    try:
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            destination.write(chunk)
    except BaseException as exc:  # noqa: BLE001
        failures.put(exc)
    finally:
        try:
            destination.close()
        except OSError:
            pass


class ParamikoSessionBackend:
    """Share one authenticated target transport across concurrent SSH calls.

    Every command or SFTP operation consumes one bounded session permit.  The
    permit count must not exceed the target sshd ``MaxSessions`` value.
    """

    def __init__(
        self,
        *,
        host: str,
        user: str | None,
        jump_host: str | None,
        jump_user: str | None,
        ssh_key_path: Path | None,
        ssh_config_path: Path | None,
        ssh_cmd: str = "ssh",
        connect_timeout: float,
        max_sessions: int,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("Paramiko max_sessions must be at least 1")
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError(
                "VB_SSH_BACKEND=paramiko requires Paramiko. "
                "Install virtuoso-bridge with `uv pip install -e '.[ssh]'`."
            ) from exc

        self._paramiko = paramiko
        self._host = host
        self._user = user
        self._jump_host = jump_host
        self._jump_user = jump_user
        self._ssh_key_path = ssh_key_path
        self._ssh_config_path = ssh_config_path
        self._ssh_cmd = ssh_cmd
        self._connect_timeout = float(connect_timeout)
        self._max_sessions = max_sessions
        self._session_gate = threading.BoundedSemaphore(max_sessions)
        self._connect_lock = threading.RLock()
        self._jump_client: Any | None = None
        self._target_client: Any | None = None
        self._jump_channel: Any | None = None
        if self._ssh_config_path is not None:
            self._ssh_config_path = self._ssh_config_path.expanduser()
            if not self._ssh_config_path.is_file():
                raise FileNotFoundError(
                    f"SSH config file not found: {self._ssh_config_path}"
                )
        self._ssh_config_cache: dict[
            tuple[str, str | None, int | None], dict[str, Any]
        ] = {}
        self._target_endpoint = self._endpoint(self._host, self._user)
        self._jump_endpoint = self._resolve_jump_endpoint()

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def _lookup(
        self,
        host: str,
        user: str | None = None,
        port: int | None = None,
    ) -> dict[str, Any]:
        cache_key = (host, user, port)
        cached = self._ssh_config_cache.get(cache_key)
        if cached is not None:
            return cached

        command = [self._ssh_cmd, "-G"]
        if self._ssh_config_path is not None:
            command.extend(["-F", str(self._ssh_config_path)])
        if user is not None:
            command.extend(["-l", user])
        if port is not None:
            command.extend(["-p", str(port)])
        if self._ssh_key_path is not None:
            command.extend(["-i", str(self._ssh_key_path.expanduser())])
        command.append(host)

        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=self._connect_timeout,
            **_windows_no_window_kwargs(),
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or b"").decode(
                "utf-8", errors="replace"
            ).strip()
            detail = f": {stderr}" if stderr else ""
            raise ValueError(
                f"OpenSSH could not resolve configuration for {host!r}{detail}"
            )

        resolved: dict[str, Any] = {}
        repeated: dict[str, list[str]] = {}
        stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
        for line in stdout.splitlines():
            key, separator, value = line.partition(" ")
            if not separator:
                continue
            normalized_key = key.strip().lower()
            normalized_value = value.strip()
            if normalized_key == "identityfile":
                repeated.setdefault(normalized_key, []).append(normalized_value)
            else:
                resolved[normalized_key] = normalized_value
        for key, values in repeated.items():
            resolved[key] = tuple(values)
        self._ssh_config_cache[cache_key] = resolved
        return resolved

    @staticmethod
    def _known_hosts_files(
        lookup: Any,
        *,
        host_alias: str,
        hostname: str,
        host_key_alias: str,
        port: int,
        username: str | None,
    ) -> tuple[Path, ...]:
        defaults = _default_known_hosts_files()
        proxy_jump = str(lookup.get("proxyjump") or "")

        def configured_paths(key: str, fallback: tuple[Path, ...]) -> tuple[Path, ...]:
            raw_value = lookup.get(key)
            if raw_value is None:
                return fallback
            if os.name == "nt":
                values = str(raw_value).split()
            else:
                values = shlex.split(str(raw_value), comments=False, posix=True)
            if len(values) == 1 and values[0].lower() == "none":
                return ()
            if any(value.lower() == "none" for value in values):
                raise ValueError(f"Invalid {key} value for host {host_alias!r}")
            return tuple(
                _expand_ssh_path(
                    value,
                    host_alias=host_alias,
                    hostname=hostname,
                    host_key_alias=host_key_alias,
                    port=port,
                    remote_user=username,
                    proxy_jump=proxy_jump,
                )
                for value in values
            )

        user_files = configured_paths("userknownhostsfile", defaults[:2])
        global_files = configured_paths("globalknownhostsfile", defaults[2:])
        return tuple(dict.fromkeys((*user_files, *global_files)))

    def _endpoint(
        self,
        host: str,
        user: str | None,
        *,
        fallback_user: str | None = None,
        port_override: int | None = None,
    ) -> _Endpoint:
        effective_user = user or fallback_user
        lookup = self._lookup(host, effective_user, port_override)
        hostname = str(lookup.get("hostname") or host)
        username = lookup.get("user") or effective_user
        try:
            port = int(
                port_override
                if port_override is not None
                else (lookup.get("port") or 22)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid SSH port for host {host!r}") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid SSH port for host {host!r}: {port}")

        raw_keys = lookup.get("identityfile") or ()
        if isinstance(raw_keys, str):
            raw_keys = (raw_keys,)
        proxy_jump = str(lookup.get("proxyjump") or "")
        host_key_alias = str(lookup.get("hostkeyalias") or hostname)
        keys = tuple(
            str(
                _expand_ssh_path(
                    str(key),
                    host_alias=host,
                    hostname=hostname,
                    host_key_alias=host_key_alias,
                    port=port,
                    remote_user=username,
                    proxy_jump=proxy_jump,
                )
            )
            for key in raw_keys
            if str(key).strip().lower() != "none"
        )

        identities_only_value = str(lookup.get("identitiesonly") or "no").lower()
        if identities_only_value not in ("yes", "no"):
            raise ValueError(
                f"Invalid IdentitiesOnly value for host {host!r}: "
                f"{identities_only_value!r}"
            )
        strict_host_key_checking = str(
            lookup.get("stricthostkeychecking") or "yes"
        ).lower()
        if strict_host_key_checking not in ("yes", "ask"):
            raise ValueError(
                "Paramiko backend requires recorded host keys; "
                f"StrictHostKeyChecking={strict_host_key_checking} is not supported "
                f"for host {host!r}"
            )
        revoked_host_keys = str(
            lookup.get("revokedhostkeys") or "none"
        ).strip()
        if revoked_host_keys.lower() != "none":
            raise ValueError(
                "Paramiko backend cannot enforce "
                f"RevokedHostKeys={revoked_host_keys!r} for host {host!r}; "
                "use the OpenSSH backend"
            )

        known_hosts_files = self._known_hosts_files(
            lookup,
            host_alias=host,
            hostname=hostname,
            host_key_alias=host_key_alias,
            port=port,
            username=username,
        )
        return _Endpoint(
            host_alias=host,
            hostname=hostname,
            username=username,
            port=port,
            key_filenames=keys,
            identities_only=identities_only_value == "yes",
            host_key_alias=host_key_alias if host_key_alias != hostname else None,
            known_hosts_files=known_hosts_files,
        )

    @staticmethod
    def _parse_proxy_jump(value: str, host: str) -> _ProxyJump | None:
        spec = value.strip()
        if not spec or spec.lower() == "none":
            return None
        if "," in spec:
            raise ValueError(
                f"Paramiko backend supports one ProxyJump hop for {host!r}; "
                f"got {spec!r}"
            )

        if spec.lower().startswith("ssh://"):
            parsed = urlsplit(spec)
            if (
                parsed.scheme.lower() != "ssh"
                or parsed.hostname is None
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"Invalid ProxyJump for host {host!r}: {spec!r}")
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError(
                    f"Invalid ProxyJump port for host {host!r}: {spec!r}"
                ) from exc
            return _ProxyJump(
                host=parsed.hostname,
                username=unquote(parsed.username) if parsed.username else None,
                port=port,
            )

        username = None
        host_and_port = spec
        if "@" in host_and_port:
            username, _separator, host_and_port = host_and_port.rpartition("@")
            if not username:
                raise ValueError(f"Invalid ProxyJump for host {host!r}: {spec!r}")

        port = None
        if host_and_port.startswith("["):
            closing = host_and_port.find("]")
            if closing < 0:
                raise ValueError(f"Invalid ProxyJump for host {host!r}: {spec!r}")
            jump_host = host_and_port[1:closing]
            suffix = host_and_port[closing + 1 :]
            if suffix:
                if not suffix.startswith(":"):
                    raise ValueError(
                        f"Invalid ProxyJump for host {host!r}: {spec!r}"
                    )
                port_text = suffix[1:]
                try:
                    port = int(port_text)
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid ProxyJump port for host {host!r}: {spec!r}"
                    ) from exc
        elif host_and_port.count(":") == 1:
            jump_host, port_text = host_and_port.rsplit(":", 1)
            try:
                port = int(port_text)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid ProxyJump port for host {host!r}: {spec!r}"
                ) from exc
        else:
            jump_host = host_and_port

        if not jump_host or (port is not None and not 1 <= port <= 65535):
            raise ValueError(f"Invalid ProxyJump for host {host!r}: {spec!r}")
        return _ProxyJump(jump_host, username, port)

    def _proxy_jump_for(
        self,
        host: str,
        user: str | None = None,
        port: int | None = None,
    ) -> _ProxyJump | None:
        lookup = self._lookup(host, user, port)
        proxy_command = lookup.get("proxycommand")
        if proxy_command is not None:
            raise ValueError(
                f"Paramiko backend does not support ProxyCommand for host {host!r}; "
                "use ProxyJump or VB_JUMP_HOST"
            )
        raw_proxy_jump = str(lookup.get("proxyjump") or "")
        if raw_proxy_jump:
            endpoint = self._endpoint(host, user, port_override=port)
            raw_proxy_jump = self._expand_connection_tokens(
                raw_proxy_jump,
                endpoint=endpoint,
            )
        return self._parse_proxy_jump(raw_proxy_jump, host)

    @staticmethod
    def _expand_connection_tokens(value: str, *, endpoint: _Endpoint) -> str:
        local_user = getpass.getuser()
        remote_user = endpoint.username or local_user
        tokens = {
            "%": "%",
            "d": str(Path.home()),
            "h": endpoint.hostname,
            "i": str(os.getuid()) if hasattr(os, "getuid") else local_user,
            "L": socket.gethostname().split(".", 1)[0],
            "l": socket.getfqdn(),
            "n": endpoint.host_alias,
            "p": str(endpoint.port),
            "r": remote_user,
            "u": local_user,
        }

        def replace_token(match: re.Match[str]) -> str:
            token = match.group(1)
            if token not in tokens:
                raise ValueError(f"Unsupported ProxyJump token %{token}")
            return tokens[token]

        return _PROXY_TOKEN_RE.sub(replace_token, value)

    def _resolve_jump_endpoint(self) -> _Endpoint | None:
        if self._jump_host is not None:
            routing_host = self._jump_host
            routing_user = self._jump_user or self._user
            routing_port = None
            jump_endpoint = self._endpoint(
                self._jump_host,
                self._jump_user,
                fallback_user=self._user,
            )
        else:
            proxy_jump = self._proxy_jump_for(self._host, self._user)
            if proxy_jump is None:
                return None
            routing_host = proxy_jump.host
            routing_user = proxy_jump.username
            routing_port = proxy_jump.port
            jump_endpoint = self._endpoint(
                proxy_jump.host,
                proxy_jump.username,
                port_override=proxy_jump.port,
            )

        nested_jump = self._proxy_jump_for(
            routing_host,
            routing_user,
            routing_port,
        )
        if nested_jump is not None:
            raise ValueError(
                "Paramiko backend supports one jump hop; "
                f"jump host {jump_endpoint.host_alias!r} configures another ProxyJump"
            )
        return jump_endpoint

    @staticmethod
    def _transport_is_ready(client: Any | None) -> bool:
        if client is None:
            return False
        transport = client.get_transport()
        return bool(
            transport is not None
            and transport.is_active()
            and transport.is_authenticated()
        )

    def _connect_client(
        self,
        endpoint: _Endpoint,
        deadline: _Deadline,
        *,
        sock: Any | None = None,
    ) -> Any:
        client = self._paramiko.SSHClient()
        for known_hosts_file in endpoint.known_hosts_files:
            if known_hosts_file.is_file():
                client.load_system_host_keys(str(known_hosts_file))
        client.set_missing_host_key_policy(self._paramiko.RejectPolicy())
        available_keys = tuple(
            key for key in endpoint.key_filenames if Path(key).is_file()
        )
        key_filename: str | list[str] | None
        if not available_keys:
            key_filename = None
        elif len(available_keys) == 1:
            key_filename = available_keys[0]
        else:
            key_filename = list(available_keys)
        connect_hostname = endpoint.host_key_alias or endpoint.hostname
        connection_socket = sock
        owned_socket = None
        try:
            if connection_socket is None and connect_hostname != endpoint.hostname:
                owned_socket = socket.create_connection(
                    (endpoint.hostname, endpoint.port),
                    timeout=deadline.remaining(endpoint.hostname),
                )
                connection_socket = owned_socket
            remaining = deadline.remaining(endpoint.hostname)
            client.connect(
                hostname=connect_hostname,
                port=endpoint.port,
                username=endpoint.username,
                key_filename=key_filename,
                sock=connection_socket,
                allow_agent=not endpoint.identities_only,
                look_for_keys=False,
                timeout=remaining,
                banner_timeout=remaining,
                auth_timeout=remaining,
                channel_timeout=remaining,
            )
        except Exception:
            client.close()
            if owned_socket is not None:
                owned_socket.close()
            raise
        transport = client.get_transport()
        if transport is None or not transport.is_authenticated():
            client.close()
            raise OSError(f"SSH authentication did not complete for {endpoint.hostname}")
        transport.set_keepalive(30)
        return client

    def ensure_connected(self, timeout: float | None = None) -> None:
        deadline = _Deadline.start(self._connect_timeout if timeout is None else timeout)
        acquired = self._connect_lock.acquire(
            timeout=deadline.remaining(self._host)
        )
        if not acquired:
            raise subprocess.TimeoutExpired(self._host, deadline.timeout)
        try:
            if self._transport_is_ready(self._target_client):
                return
            self._close_locked()

            target_endpoint = self._target_endpoint
            jump_endpoint = self._jump_endpoint
            jump_client = None
            jump_channel = None
            target_client = None
            try:
                if jump_endpoint is not None:
                    jump_client = self._connect_client(jump_endpoint, deadline)
                    jump_transport = jump_client.get_transport()
                    if jump_transport is None:
                        raise OSError("Jump-host SSH transport is unavailable")
                    jump_channel = jump_transport.open_channel(
                        "direct-tcpip",
                        (target_endpoint.hostname, target_endpoint.port),
                        ("127.0.0.1", 0),
                        timeout=deadline.remaining(target_endpoint.hostname),
                    )
                target_client = self._connect_client(
                    target_endpoint,
                    deadline,
                    sock=jump_channel,
                )
            except Exception:
                if target_client is not None:
                    target_client.close()
                if jump_channel is not None:
                    jump_channel.close()
                if jump_client is not None:
                    jump_client.close()
                raise

            self._jump_client = jump_client
            self._jump_channel = jump_channel
            self._target_client = target_client
            logger.info(
                "Paramiko transport connected to %s via %s (max_sessions=%d)",
                self._host,
                jump_endpoint.host_alias if jump_endpoint is not None else "direct",
                self._max_sessions,
            )
        finally:
            self._connect_lock.release()

    def test_connection(self, timeout: float | None = None) -> bool:
        try:
            self.ensure_connected(timeout)
            return True
        except (
            OSError,
            socket.error,
            subprocess.TimeoutExpired,
            self._paramiko.SSHException,
        ) as exc:
            logger.warning("Paramiko connection to %s failed: %s", self._host, exc)
            return False

    def _target_transport(self) -> Any:
        client = self._target_client
        if not self._transport_is_ready(client):
            raise OSError("Paramiko target transport is not connected")
        transport = client.get_transport()
        if transport is None:
            raise OSError("Paramiko target transport is unavailable")
        return transport

    @contextmanager
    def _session_lease(self, deadline: _Deadline, command: object) -> Iterator[Any]:
        self.ensure_connected(deadline.remaining(command))
        acquired = self._session_gate.acquire(timeout=deadline.remaining(command))
        if not acquired:
            raise subprocess.TimeoutExpired(command, deadline.timeout)
        try:
            yield self._target_transport()
        finally:
            self._session_gate.release()

    @staticmethod
    def _decode(chunks: list[bytes]) -> str:
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _collect_channel(
        self,
        channel: Any,
        deadline: _Deadline,
        command: object,
    ) -> tuple[int, str, str]:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        while True:
            while channel.recv_ready():
                stdout_chunks.append(channel.recv(65536))
            while channel.recv_stderr_ready():
                stderr_chunks.append(channel.recv_stderr(65536))
            if (
                channel.exit_status_ready()
                and (channel.eof_received or channel.closed)
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break
            deadline.remaining(command)
            time.sleep(0.01)
        return (
            channel.recv_exit_status(),
            self._decode(stdout_chunks),
            self._decode(stderr_chunks),
        )

    def run_command(self, command: str, timeout: float) -> tuple[int, str, str]:
        deadline = _Deadline.start(timeout)
        try:
            with self._session_lease(deadline, command) as transport:
                channel = transport.open_session(timeout=deadline.remaining(command))
                try:
                    channel.settimeout(deadline.remaining(command))
                    channel.exec_command("sh -l")
                    channel.sendall(command.encode("utf-8"))
                    channel.shutdown_write()
                    return self._collect_channel(channel, deadline, command)
                finally:
                    channel.close()
        except subprocess.TimeoutExpired:
            raise
        except socket.timeout as exc:
            raise subprocess.TimeoutExpired(command, deadline.timeout) from exc
        except Exception as exc:  # noqa: BLE001
            self._invalidate_if_transport_failed(exc)
            return 255, "", str(exc)

    @staticmethod
    def _wait_tar_transfer(
        channel: Any,
        process: subprocess.Popen[Any],
        workers: list[threading.Thread],
        failures: "queue.Queue[BaseException]",
        deadline: _Deadline,
        command: object,
    ) -> tuple[int, int]:
        while True:
            try:
                worker_failure = failures.get_nowait()
            except queue.Empty:
                pass
            else:
                raise worker_failure
            if (
                channel.exit_status_ready()
                and process.poll() is not None
                and all(not worker.is_alive() for worker in workers)
            ):
                break
            deadline.remaining(command)
            time.sleep(0.01)
        try:
            worker_failure = failures.get_nowait()
        except queue.Empty:
            pass
        else:
            raise worker_failure
        remote_rc = channel.recv_exit_status()
        local_rc = process.wait(timeout=deadline.remaining(command))
        return remote_rc, local_rc

    @staticmethod
    def _stop_tar_transfer(
        channel: Any | None,
        process: subprocess.Popen[Any] | None,
        streams: list[Any],
        workers: list[threading.Thread],
    ) -> None:
        if channel is not None:
            channel.close()
        if process is not None and process.poll() is None:
            process.kill()
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass
        if process is not None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        for worker in workers:
            worker.join(timeout=1)

    @staticmethod
    def _tar_result(
        remote_rc: int,
        local_rc: int,
        remote_stdout: list[bytes],
        remote_stderr: list[bytes],
        local_stderr: list[bytes],
    ) -> tuple[int, str, str]:
        stderr_parts = []
        remote_error = ParamikoSessionBackend._decode(remote_stderr).strip()
        local_error = ParamikoSessionBackend._decode(local_stderr).strip()
        if remote_error:
            stderr_parts.append(f"Remote tar error: {remote_error}")
        if local_error:
            stderr_parts.append(f"Local tar error: {local_error}")
        return (
            remote_rc or local_rc,
            ParamikoSessionBackend._decode(remote_stdout),
            " | ".join(stderr_parts),
        )

    def upload_tar(
        self,
        plan: TarUploadPlan,
        *,
        timeout: float,
    ) -> tuple[int, str, str]:
        """Execute a shared tar upload plan on one Paramiko session channel."""
        deadline = _Deadline.start(timeout)
        channel = None
        tar_process: subprocess.Popen[Any] | None = None
        streams: list[Any] = []
        workers: list[threading.Thread] = []
        failures: "queue.Queue[BaseException]" = queue.Queue()
        try:
            with self._session_lease(deadline, plan.remote_command) as transport:
                channel = transport.open_session(
                    timeout=deadline.remaining(plan.remote_command)
                )
                try:
                    channel.settimeout(deadline.remaining(plan.remote_command))
                    channel.exec_command(plan.remote_command)
                    tar_process = subprocess.Popen(
                        list(plan.local_command),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        **_windows_no_window_kwargs(),
                    )
                    if tar_process.stdout is None or tar_process.stderr is None:
                        raise OSError("Failed to allocate local tar pipes")

                    remote_stdout_file = channel.makefile("rb")
                    remote_stderr_file = channel.makefile_stderr("rb")
                    streams.extend(
                        [
                            tar_process.stdout,
                            tar_process.stderr,
                            remote_stdout_file,
                            remote_stderr_file,
                        ]
                    )
                    remote_stdout: list[bytes] = []
                    remote_stderr: list[bytes] = []
                    local_stderr: list[bytes] = []
                    workers = [
                        threading.Thread(
                            target=_send_stream_to_channel,
                            args=(tar_process.stdout, channel, failures),
                            daemon=True,
                        ),
                        threading.Thread(
                            target=_read_stream,
                            args=(tar_process.stderr, local_stderr, failures),
                            daemon=True,
                        ),
                        threading.Thread(
                            target=_read_stream,
                            args=(remote_stdout_file, remote_stdout, failures),
                            daemon=True,
                        ),
                        threading.Thread(
                            target=_read_stream,
                            args=(remote_stderr_file, remote_stderr, failures),
                            daemon=True,
                        ),
                    ]
                    for worker in workers:
                        worker.start()
                    remote_rc, local_rc = self._wait_tar_transfer(
                        channel,
                        tar_process,
                        workers,
                        failures,
                        deadline,
                        plan.remote_command,
                    )
                finally:
                    self._stop_tar_transfer(
                        channel,
                        tar_process,
                        streams,
                        workers,
                    )
            return self._tar_result(
                remote_rc,
                local_rc,
                remote_stdout,
                remote_stderr,
                local_stderr,
            )
        except subprocess.TimeoutExpired:
            raise
        except socket.timeout as exc:
            raise subprocess.TimeoutExpired(
                plan.remote_command,
                deadline.timeout,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            self._invalidate_if_transport_failed(exc)
            return 255, "", str(exc)

    def download_tar(
        self,
        plan: TarDownloadPlan,
        *,
        timeout: float,
    ) -> tuple[int, str, str]:
        """Execute a shared recursive tar download plan on one session."""
        deadline = _Deadline.start(timeout)
        channel = None
        tar_process: subprocess.Popen[Any] | None = None
        streams: list[Any] = []
        workers: list[threading.Thread] = []
        failures: "queue.Queue[BaseException]" = queue.Queue()
        plan.local_path.parent.mkdir(parents=True, exist_ok=True)
        plan.stage_path.mkdir(parents=True)
        try:
            with self._session_lease(deadline, plan.remote_command) as transport:
                channel = transport.open_session(
                    timeout=deadline.remaining(plan.remote_command)
                )
                try:
                    channel.settimeout(deadline.remaining(plan.remote_command))
                    channel.exec_command(plan.remote_command)
                    tar_process = subprocess.Popen(
                        list(plan.local_command),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        cwd=plan.stage_path,
                        **_windows_no_window_kwargs(),
                    )
                    if tar_process.stdin is None or tar_process.stderr is None:
                        raise OSError("Failed to allocate local tar pipes")

                    remote_stdout_file = channel.makefile("rb")
                    remote_stderr_file = channel.makefile_stderr("rb")
                    streams.extend(
                        [
                            tar_process.stdin,
                            tar_process.stderr,
                            remote_stdout_file,
                            remote_stderr_file,
                        ]
                    )
                    remote_stderr: list[bytes] = []
                    local_stderr: list[bytes] = []
                    workers = [
                        threading.Thread(
                            target=_copy_stream,
                            args=(remote_stdout_file, tar_process.stdin, failures),
                            daemon=True,
                        ),
                        threading.Thread(
                            target=_read_stream,
                            args=(remote_stderr_file, remote_stderr, failures),
                            daemon=True,
                        ),
                        threading.Thread(
                            target=_read_stream,
                            args=(tar_process.stderr, local_stderr, failures),
                            daemon=True,
                        ),
                    ]
                    for worker in workers:
                        worker.start()
                    remote_rc, local_rc = self._wait_tar_transfer(
                        channel,
                        tar_process,
                        workers,
                        failures,
                        deadline,
                        plan.remote_command,
                    )
                finally:
                    self._stop_tar_transfer(
                        channel,
                        tar_process,
                        streams,
                        workers,
                    )
            result = self._tar_result(
                remote_rc,
                local_rc,
                [],
                remote_stderr,
                local_stderr,
            )
            if result[0] != 0:
                discard_stage(plan.stage_path)
                return result
            if not (plan.staged_item.exists() or plan.staged_item.is_symlink()):
                discard_stage(plan.stage_path)
                return (
                    1,
                    "",
                    "Downloaded archive did not contain expected directory: "
                    f"{plan.staged_item.name}",
                )
            install_staged_path(plan)
            return result
        except subprocess.TimeoutExpired:
            discard_stage(plan.stage_path)
            raise
        except socket.timeout as exc:
            discard_stage(plan.stage_path)
            raise subprocess.TimeoutExpired(
                plan.remote_command,
                deadline.timeout,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            discard_stage(plan.stage_path)
            self._invalidate_if_transport_failed(exc)
            return 255, "", str(exc)

    @staticmethod
    def _error_result(exc: Exception) -> tuple[int, str, str]:
        returncode = 1 if getattr(exc, "errno", None) == errno.ENOENT else 255
        return returncode, "", str(exc)

    @contextmanager
    def _sftp(self, deadline: _Deadline, command: object) -> Iterator[Any]:
        with self._session_lease(deadline, command) as transport:
            channel = transport.open_session(timeout=deadline.remaining(command))
            try:
                channel.settimeout(deadline.remaining(command))
                channel.invoke_subsystem("sftp")
                sftp = self._paramiko.SFTPClient(channel)
                try:
                    yield sftp
                finally:
                    sftp.close()
            except Exception:
                channel.close()
                raise

    def upload_text(
        self,
        plan: TextUploadPlan,
        payload: bytes,
        *,
        timeout: float,
    ) -> tuple[int, str, str]:
        deadline = _Deadline.start(timeout)
        try:
            with self._session_lease(deadline, plan.remote_command) as transport:
                channel = transport.open_session(
                    timeout=deadline.remaining(plan.remote_command)
                )
                try:
                    channel.settimeout(deadline.remaining(plan.remote_command))
                    channel.exec_command(plan.remote_command)
                    channel.sendall(payload)
                    channel.shutdown_write()
                    return self._collect_channel(
                        channel,
                        deadline,
                        plan.remote_command,
                    )
                finally:
                    channel.close()
        except subprocess.TimeoutExpired:
            raise
        except socket.timeout as exc:
            raise subprocess.TimeoutExpired(
                plan.remote_command,
                deadline.timeout,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            self._invalidate_if_transport_failed(exc)
            return self._error_result(exc)

    def download_file(
        self,
        plan: FileDownloadPlan,
        *,
        timeout: float,
    ) -> tuple[int, str, str]:
        deadline = _Deadline.start(timeout)
        plan.local_path.parent.mkdir(parents=True, exist_ok=True)
        plan.stage_path.mkdir(parents=True)
        try:
            with self._sftp(deadline, plan.remote_path) as sftp:
                sftp.get(
                    plan.remote_path,
                    str(plan.staged_item),
                    callback=lambda _received, _total: deadline.remaining(
                        plan.remote_path
                    ),
                )
            install_staged_item(
                plan.stage_path,
                plan.staged_item,
                plan.local_path,
            )
            return 0, "", ""
        except subprocess.TimeoutExpired:
            discard_stage(plan.stage_path)
            raise
        except socket.timeout as exc:
            discard_stage(plan.stage_path)
            raise subprocess.TimeoutExpired(
                plan.remote_path,
                deadline.timeout,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            discard_stage(plan.stage_path)
            self._invalidate_if_transport_failed(exc)
            return self._error_result(exc)

    def _invalidate_if_transport_failed(self, _exc: Exception) -> None:
        with self._connect_lock:
            target_failed = not self._transport_is_ready(self._target_client)
            jump_failed = (
                self._jump_client is not None
                and not self._transport_is_ready(self._jump_client)
            )
            if target_failed or jump_failed:
                self._close_locked()

    def _close_locked(self) -> None:
        if self._target_client is not None:
            self._target_client.close()
        if self._jump_channel is not None:
            self._jump_channel.close()
        if self._jump_client is not None:
            self._jump_client.close()
        self._target_client = None
        self._jump_channel = None
        self._jump_client = None

    def close(self) -> None:
        with self._connect_lock:
            self._close_locked()

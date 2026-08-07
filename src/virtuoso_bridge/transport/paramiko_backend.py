"""Persistent Paramiko SSH transport with bounded session multiplexing."""

from __future__ import annotations

import errno
import logging
import os
import posixpath
import shutil
import socket
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

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
    hostname: str
    username: str | None
    port: int
    key_filenames: tuple[str, ...]


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
        self._jump_user = jump_user or user
        self._ssh_key_path = ssh_key_path
        self._ssh_config_path = ssh_config_path
        self._connect_timeout = float(connect_timeout)
        self._max_sessions = max_sessions
        self._session_gate = threading.BoundedSemaphore(max_sessions)
        self._connect_lock = threading.RLock()
        self._jump_client: Any | None = None
        self._target_client: Any | None = None
        self._jump_channel: Any | None = None
        self._ssh_config = self._load_ssh_config()

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def _load_ssh_config(self) -> Any | None:
        path = self._ssh_config_path
        if path is None:
            candidate = Path.home() / ".ssh" / "config"
            path = candidate if candidate.is_file() else None
        if path is None or not path.is_file():
            return None
        config = self._paramiko.SSHConfig()
        with path.open("r", encoding="utf-8") as handle:
            config.parse(handle)
        return config

    def _endpoint(self, host: str, user: str | None) -> _Endpoint:
        lookup = self._ssh_config.lookup(host) if self._ssh_config is not None else {}
        hostname = str(lookup.get("hostname") or host)
        username = user or lookup.get("user")
        port = int(lookup.get("port") or 22)

        if self._ssh_key_path is not None:
            keys = (str(self._ssh_key_path.expanduser()),)
        else:
            raw_keys = lookup.get("identityfile") or ()
            if isinstance(raw_keys, str):
                raw_keys = (raw_keys,)
            keys = tuple(os.path.expandvars(os.path.expanduser(str(key))) for key in raw_keys)
        return _Endpoint(hostname, username, port, keys)

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
        client.load_system_host_keys()
        for system_known_hosts in (
            Path("/etc/ssh/ssh_known_hosts"),
            Path("/etc/ssh/ssh_known_hosts2"),
        ):
            if system_known_hosts.is_file():
                client.load_system_host_keys(str(system_known_hosts))
        # Preserve the OpenSSH backend's permissive first connection while
        # still rejecting changes to keys already recorded in known_hosts.
        client.set_missing_host_key_policy(self._paramiko.AutoAddPolicy())
        remaining = deadline.remaining(endpoint.hostname)
        key_filename: str | list[str] | None
        if not endpoint.key_filenames:
            key_filename = None
        elif len(endpoint.key_filenames) == 1:
            key_filename = endpoint.key_filenames[0]
        else:
            key_filename = list(endpoint.key_filenames)
        try:
            client.connect(
                hostname=endpoint.hostname,
                port=endpoint.port,
                username=endpoint.username,
                key_filename=key_filename,
                sock=sock,
                allow_agent=True,
                look_for_keys=True,
                timeout=remaining,
                banner_timeout=remaining,
                auth_timeout=remaining,
                channel_timeout=remaining,
            )
        except Exception:
            client.close()
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

            target_endpoint = self._endpoint(self._host, self._user)
            jump_client = None
            jump_channel = None
            target_client = None
            try:
                if self._jump_host:
                    jump_endpoint = self._endpoint(self._jump_host, self._jump_user)
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
                self._jump_host or "direct",
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
        except Exception as exc:  # noqa: BLE001
            self._invalidate_if_transport_failed(exc)
            return 255, "", str(exc)

    @staticmethod
    def _mkdir_p(
        sftp: Any,
        remote_dir: str,
        deadline: _Deadline,
    ) -> None:
        if remote_dir in ("", ".", "/"):
            return
        current = "/" if remote_dir.startswith("/") else ""
        for part in PurePosixPath(remote_dir).parts:
            if part in ("", "/", "."):
                continue
            current = posixpath.join(current, part) if current else part
            deadline.remaining(current)
            try:
                sftp.stat(current)
            except OSError as exc:
                if getattr(exc, "errno", None) not in (None, errno.ENOENT):
                    raise
                try:
                    sftp.mkdir(current)
                except OSError as mkdir_exc:
                    try:
                        attrs = sftp.stat(current)
                    except OSError:
                        raise mkdir_exc
                    if not stat.S_ISDIR(attrs.st_mode):
                        raise mkdir_exc

    @classmethod
    def _upload_path(
        cls,
        sftp: Any,
        local_path: Path,
        remote_path: str,
        deadline: _Deadline,
    ) -> None:
        deadline.remaining(remote_path)
        if local_path.is_dir():
            cls._mkdir_p(sftp, remote_path, deadline)
            for child in local_path.iterdir():
                cls._upload_path(
                    sftp,
                    child,
                    posixpath.join(remote_path, child.name),
                    deadline,
                )
            return
        cls._mkdir_p(sftp, posixpath.dirname(remote_path), deadline)
        sftp.put(
            str(local_path),
            remote_path,
            callback=lambda _sent, _total: deadline.remaining(remote_path),
        )

    @classmethod
    def _download_path(
        cls,
        sftp: Any,
        remote_path: str,
        local_path: Path,
        deadline: _Deadline,
    ) -> None:
        deadline.remaining(remote_path)
        attrs = sftp.lstat(remote_path)
        if stat.S_ISDIR(attrs.st_mode):
            local_path.mkdir(parents=True, exist_ok=True)
            for child in sftp.listdir_attr(remote_path):
                cls._download_path(
                    sftp,
                    posixpath.join(remote_path, child.filename),
                    local_path / child.filename,
                    deadline,
                )
            return
        if stat.S_ISLNK(attrs.st_mode):
            target = sftp.readlink(remote_path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.symlink_to(target)
            return
        local_path.parent.mkdir(parents=True, exist_ok=True)
        sftp.get(
            remote_path,
            str(local_path),
            callback=lambda _received, _total: deadline.remaining(remote_path),
        )

    @staticmethod
    def _replace_download(stage_path: Path, staged_item: Path, local_path: Path) -> None:
        backup_path: Path | None = None
        try:
            if local_path.exists() or local_path.is_symlink():
                backup_path = local_path.parent / f".vbbak-{uuid.uuid4().hex}"
                local_path.rename(backup_path)
            staged_item.rename(local_path)
        except Exception:
            if stage_path.exists():
                shutil.rmtree(stage_path, ignore_errors=True)
            if (
                backup_path is not None
                and not (local_path.exists() or local_path.is_symlink())
                and (backup_path.exists() or backup_path.is_symlink())
            ):
                backup_path.rename(local_path)
            raise
        else:
            if backup_path is not None and (backup_path.exists() or backup_path.is_symlink()):
                if backup_path.is_dir() and not backup_path.is_symlink():
                    shutil.rmtree(backup_path)
                else:
                    backup_path.unlink()
            shutil.rmtree(stage_path, ignore_errors=True)

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

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        *,
        timeout: float,
    ) -> tuple[int, str, str]:
        deadline = _Deadline.start(timeout)
        try:
            with self._sftp(deadline, remote_path) as sftp:
                self._upload_path(sftp, local_path, remote_path, deadline)
            return 0, "", ""
        except subprocess.TimeoutExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            self._invalidate_if_transport_failed(exc)
            return self._error_result(exc)

    def upload_batch(
        self,
        files: list[tuple[Path, str]],
        *,
        timeout: float,
    ) -> tuple[int, str, str]:
        if not files:
            return 0, "", ""
        deadline = _Deadline.start(timeout)
        try:
            with self._sftp(deadline, "upload_batch") as sftp:
                for local_path, remote_path in files:
                    self._upload_path(sftp, local_path, remote_path, deadline)
            return 0, "", ""
        except subprocess.TimeoutExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            self._invalidate_if_transport_failed(exc)
            return self._error_result(exc)

    def upload_text(
        self,
        text: str,
        remote_path: str,
        *,
        timeout: float,
    ) -> tuple[int, str, str]:
        deadline = _Deadline.start(timeout)
        try:
            with self._sftp(deadline, remote_path) as sftp:
                self._mkdir_p(sftp, posixpath.dirname(remote_path), deadline)
                with sftp.file(remote_path, "wb") as remote_file:
                    remote_file.settimeout(deadline.remaining(remote_path))
                    remote_file.write(text.encode("utf-8"))
            return 0, "", ""
        except subprocess.TimeoutExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            self._invalidate_if_transport_failed(exc)
            return self._error_result(exc)

    def download(
        self,
        remote_path: str,
        local_path: Path,
        *,
        recursive: bool,
        timeout: float,
    ) -> tuple[int, str, str]:
        deadline = _Deadline.start(timeout)
        try:
            if not recursive:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                stage_path = local_path.parent / f".vbtmp-{uuid.uuid4().hex}"
                staged_item = stage_path / local_path.name
                stage_path.mkdir(parents=True)
                try:
                    with self._sftp(deadline, remote_path) as sftp:
                        sftp.get(
                            remote_path,
                            str(staged_item),
                            callback=lambda _received, _total: deadline.remaining(remote_path),
                        )
                    self._replace_download(stage_path, staged_item, local_path)
                except Exception:
                    shutil.rmtree(stage_path, ignore_errors=True)
                    raise
                return 0, "", ""

            local_path.parent.mkdir(parents=True, exist_ok=True)
            stage_path = local_path.parent / f".vbtmp-{uuid.uuid4().hex}"
            staged_item = stage_path / local_path.name
            stage_path.mkdir(parents=True)
            try:
                with self._sftp(deadline, remote_path) as sftp:
                    self._download_path(sftp, remote_path, staged_item, deadline)
                self._replace_download(stage_path, staged_item, local_path)
            except Exception:
                shutil.rmtree(stage_path, ignore_errors=True)
                raise
            return 0, "", ""
        except subprocess.TimeoutExpired:
            raise
        except Exception as exc:  # noqa: BLE001
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

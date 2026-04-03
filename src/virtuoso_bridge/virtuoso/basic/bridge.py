"""VirtuosoClient – Python client for Virtuoso SKILL execution."""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import hashlib
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from virtuoso_bridge.virtuoso.basic.composition import compose_skill_script
from virtuoso_bridge.models import ExecutionStatus, VirtuosoInterface, VirtuosoResult
from virtuoso_bridge.virtuoso.ops import (
    close_current_cellview as op_close_current_cellview,
    default_view_type_for,
    escape_skill_string,
    open_cell_view as op_open_cell_view,
    open_window as op_open_window,
    save_current_cellview as op_save_current_cellview,
)
from virtuoso_bridge.virtuoso.layout import LayoutOps
from virtuoso_bridge.virtuoso.schematic import SchematicOps

logger = logging.getLogger(__name__)

_STX = "\x02"
_NAK = "\x15"
_RECV_BUF_SIZE = 1024 * 1024
_TUNNEL_CONNECT_RETRY_DELAY = 0.2
_TUNNEL_CONNECT_GRACE_SECONDS = 3.0


def _default_remote_port(username: str | None = None) -> int:
    """Return a stable per-user default port in the range 65000-65499."""
    user = username or os.getenv("VB_REMOTE_USER", "").strip()
    if not user:
        return 65432
    return 65000 + (sum(ord(c) for c in user) % 500)


def _path_to_posix(path: str | Path) -> str:
    return Path(path).as_posix()


def _escape_skill_string(s: str) -> str:
    return escape_skill_string(s)


def _escape_for_skill_evalstring_source(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "")
        .replace("\n", "\\n")
    )


# ---------------------------------------------------------------------------
# VirtuosoClient — pure SKILL execution client
# ---------------------------------------------------------------------------

class VirtuosoClient(VirtuosoInterface):
    """Virtuoso SKILL bridge using the RAMIC daemon over TCP.

    This class only handles TCP communication with the daemon.
    SSH tunnel management is handled separately by SSHClient.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 65432,
        timeout: int = 30,
        tunnel: Any = None,
        log_to_ciw: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._tunnel = tunnel  # SSHClient, if provided
        self._log_to_ciw = log_to_ciw
        self.layout = LayoutOps(self)
        self.schematic = SchematicOps(self)
        self._il_upload_cache: dict[str, tuple[str, str]] = {}
        # For connect retry when jump host adds latency
        self._has_jump_host = (
            bool(os.getenv("VB_JUMP_HOST", "").strip())
            or (tunnel is not None and getattr(tunnel, '_jump_host', None))
        )

    # -- factory methods ----------------------------------------------------

    @classmethod
    def from_env(
        cls,
        *,
        timeout: int = 30,
        log_to_ciw: bool = True,
    ) -> "VirtuosoClient":
        """Create a VirtuosoClient from environment variables.

        If an SSH tunnel is already running (via `virtuoso-bridge start`),
        connects to its port. Otherwise creates a new SSHClient.
        """
        load_dotenv()
        from virtuoso_bridge.transport.tunnel import SSHClient

        # Check if tunnel is already running
        if SSHClient.is_running():
            state = SSHClient.read_state()
            port = state["port"]
            # Create SSHClient for file transfer ops (reuses existing tunnel)
            ssh = SSHClient.from_env(keep_remote_files=True)
            return cls(host="127.0.0.1", port=port, timeout=timeout, tunnel=ssh, log_to_ciw=log_to_ciw)

        # No tunnel running — start one
        remote_host = os.getenv("VB_REMOTE_HOST", "").strip()
        if not remote_host:
            raise RuntimeError("VB_REMOTE_HOST must be set. Run: virtuoso-bridge init")

        ssh = SSHClient.from_env(keep_remote_files=True)
        return cls(host="127.0.0.1", port=ssh.port, timeout=timeout, tunnel=ssh, log_to_ciw=log_to_ciw)

    @classmethod
    def local(
        cls,
        host: str = "127.0.0.1",
        port: int = 65432,
        timeout: int = 30,
    ) -> "VirtuosoClient":
        """Create a bridge for a locally running daemon."""
        return cls(host=host, port=port, timeout=timeout)

    @classmethod
    def from_tunnel(
        cls,
        tunnel: Any,
        timeout: int = 30,
        log_to_ciw: bool = True,
    ) -> "VirtuosoClient":
        """Create a bridge connected through a SSHClient."""
        return cls(
            host="127.0.0.1",
            port=tunnel.port,
            timeout=timeout,
            tunnel=tunnel,
            log_to_ciw=log_to_ciw,
        )

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "VirtuosoClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- properties ---------------------------------------------------------

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def remote_host(self) -> str | None:
        return getattr(self._tunnel, 'remote_host', None) if self._tunnel else None

    @property
    def is_remote(self) -> bool:
        return self._tunnel is not None

    @property
    def is_tunnel_alive(self) -> bool:
        return self._tunnel is not None and getattr(self._tunnel, 'is_tunnel_alive', False)

    @property
    def log_to_ciw(self) -> bool:
        return self._log_to_ciw

    @log_to_ciw.setter
    def log_to_ciw(self, value: bool) -> None:
        self._log_to_ciw = bool(value)

    # -- VirtuosoInterface --------------------------------------------------

    def ensure_ready(self, timeout: int = 10) -> VirtuosoResult:
        """Ensure the daemon is reachable via TCP."""
        metadata: dict[str, Any] = {}

        # If we have a tunnel, make sure it's up
        if self._tunnel is not None:
            try:
                self._tunnel.warm()
                metadata["tunnel_alive"] = self.is_tunnel_alive
                self._port = self._tunnel.port  # may have changed due to port auto-retry
            except Exception as e:
                return VirtuosoResult(
                    status=ExecutionStatus.ERROR,
                    errors=[f"Tunnel setup failed: {e}"],
                    metadata=metadata,
                )

        report = self.verify_tunnel(timeout=timeout)
        metadata["diagnostics"] = report
        errors: list[str] = []
        if not report["tcp_reachable"]:
            errors.append("TCP connection to daemon host:port failed")
        if not report["daemon_responsive"]:
            errors.append("Daemon did not respond to ping (ensure load() in CIW)")

        if errors:
            if self._tunnel and self._tunnel.setup_path and not report.get("daemon_responsive"):
                print(f'\nPlease execute in Virtuoso CIW: load("{self._tunnel.setup_path}")\n')
            return VirtuosoResult(status=ExecutionStatus.ERROR, errors=errors, metadata=metadata)
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, metadata=metadata)

    def warm_remote_session(self, timeout: int = 10) -> VirtuosoResult:
        """Warm up remote transports without requiring the daemon to answer."""
        if self._tunnel is None:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS)
        try:
            self._tunnel.warm(timeout=timeout)
            self._port = self._tunnel.port
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                metadata={"tunnel_alive": self.is_tunnel_alive},
            )
        except Exception as e:
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[str(e)],
            )

    # -- SKILL execution ----------------------------------------------------

    def execute_skill(self, skill_code: str, timeout: int | None = None) -> VirtuosoResult:
        """Execute SKILL code in Virtuoso via the RAMIC Bridge daemon."""
        effective_timeout = timeout if timeout is not None else self._timeout

        start_time = time.monotonic()
        connect_deadline = start_time
        if self._has_jump_host:
            connect_deadline = start_time + _TUNNEL_CONNECT_GRACE_SECONDS

        try:
            while True:
                try:
                    raw_response = self._execute_skill_once(skill_code, effective_timeout)
                    elapsed = time.monotonic() - start_time
                    return self._parse_response(raw_response, elapsed)
                except ConnectionRefusedError:
                    if time.monotonic() >= connect_deadline:
                        raise
                    time.sleep(_TUNNEL_CONNECT_RETRY_DELAY)
                except OSError as exc:
                    if not self._should_retry_tunnel_connect(exc, time.monotonic(), connect_deadline):
                        raise
                    time.sleep(_TUNNEL_CONNECT_RETRY_DELAY)

        except socket.timeout:
            elapsed = time.monotonic() - start_time
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[f"Socket timeout after {effective_timeout}s"],
                execution_time=elapsed,
            )
        except ConnectionRefusedError:
            elapsed = time.monotonic() - start_time
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[
                    f"Connection refused to {self._host}:{self._port}. "
                    "Ensure the RAMIC Bridge daemon is running in Virtuoso."
                ],
                execution_time=elapsed,
            )
        except OSError as exc:
            elapsed = time.monotonic() - start_time
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[f"Socket error: {exc}"],
                execution_time=elapsed,
            )

    def test_connection(self, timeout: int = 10) -> bool:
        """Test whether the daemon is reachable."""
        result = self.execute_skill("1+1", timeout=timeout)
        return result.status == ExecutionStatus.SUCCESS

    def verify_tunnel(self, timeout: int = 5) -> dict[str, Any]:
        """Diagnose connectivity."""
        report: dict[str, Any] = {
            "tunnel_process_alive": None,
            "tcp_reachable": False,
            "daemon_responsive": False,
            "daemon_output": "",
            "summary": "",
        }

        if self._tunnel is not None:
            report["tunnel_process_alive"] = self.is_tunnel_alive

        result = self.execute_skill("1+1", timeout=timeout)
        report["daemon_responsive"] = result.status == ExecutionStatus.SUCCESS
        report["daemon_output"] = result.output
        report["tcp_reachable"] = not any(
            err.startswith("Connection refused to ") or err.startswith("Socket error:")
            for err in result.errors
        )

        parts: list[str] = []
        if report["tunnel_process_alive"] is True:
            parts.append("tunnel: alive")
        elif report["tunnel_process_alive"] is False:
            parts.append("tunnel: DEAD")
        else:
            parts.append("tunnel: not managed (local)")
        parts.append(
            f"tcp {self._host}:{self._port}: "
            + ("OK" if report["tcp_reachable"] else "UNREACHABLE")
        )
        parts.append("daemon: " + ("OK" if report["daemon_responsive"] else "NO RESPONSE"))
        report["summary"] = " | ".join(parts)
        return report

    # -- cellview operations ------------------------------------------------

    def open_cell_view(self, lib: str, cell: str, *, view: str | None = None,
                       view_type: str | None = None, mode: str = "a",
                       timeout: int | None = None) -> VirtuosoResult:
        effective_timeout = timeout if timeout is not None else self._timeout
        actual_view = view or "layout"
        actual_view_type = view_type or default_view_type_for(actual_view)
        skill = op_open_cell_view(lib, cell, actual_view, actual_view_type, mode)
        return self.execute_skill(skill, timeout=effective_timeout)

    def open_window(self, lib: str, cell: str, *, view: str = "layout",
                    view_type: str | None = None, timeout: int | None = None) -> VirtuosoResult:
        effective_timeout = timeout if timeout is not None else self._timeout
        skill = op_open_window(lib, cell, view=view, view_type=view_type)
        return self.execute_skill(skill, timeout=effective_timeout)

    def save_current_cellview(self, timeout: int | None = None) -> VirtuosoResult:
        effective_timeout = timeout if timeout is not None else self._timeout
        return self.execute_skill(op_save_current_cellview(), timeout=effective_timeout)

    def close_current_cellview(self, timeout: int | None = None) -> VirtuosoResult:
        effective_timeout = timeout if timeout is not None else self._timeout
        return self.execute_skill(op_close_current_cellview(), timeout=effective_timeout)

    def get_current_design(self, timeout: int | None = None) -> tuple[str | None, str | None, str | None]:
        effective_timeout = timeout if timeout is not None else self._timeout
        skill = (
            "ddGetObjReadPath(dbGetCellViewDdId(geGetEditCellView())))"
        )
        result = self.execute_skill(skill, timeout=effective_timeout)
        if result.status != ExecutionStatus.SUCCESS:
            return None, None, None
        output = (result.output or "").strip()
        if not output or output.lower() == "nil":
            return None, None, None
        parts = output.split("/")
        if len(parts) < 4:
            return None, None, None
        return parts[-4], parts[-3], parts[-2]

    def ciw_print(self, message: str, timeout: int | None = None) -> VirtuosoResult:
        effective_timeout = timeout if timeout is not None else self._timeout
        escaped = _escape_skill_string(message)
        return self.execute_skill(f'printf("{escaped}\\n")', timeout=effective_timeout)

    def ciw_log(self, skill_code: str, timeout: int | None = None) -> VirtuosoResult:
        effective_timeout = timeout if timeout is not None else self._timeout
        return self.execute_skill(skill_code, timeout=effective_timeout)

    def run_shell_command(self, cmd: str, timeout: int | None = None) -> VirtuosoResult:
        effective_timeout = timeout if timeout is not None else self._timeout
        escaped = _escape_skill_string(cmd)
        result = self.execute_skill(f'csh("{escaped}")', timeout=effective_timeout)
        if result.status != ExecutionStatus.SUCCESS:
            return result
        if (result.output or "").strip().lower() == "nil":
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[f"Shell command failed (csh returned nil): {cmd}"],
                metadata={"cmd": cmd},
                execution_time=result.execution_time,
            )
        return result

    # -- file transfer (delegates to tunnel) --------------------------------

    def download_file(self, remote_path: str | Path, local_path: str | Path,
                      *, timeout: int | None = None) -> VirtuosoResult:
        started = time.perf_counter()
        source = _path_to_posix(remote_path)
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if self._tunnel is not None:
            result = self._tunnel.download_file(source, destination, timeout=timeout)
            elapsed = time.perf_counter() - started
            if result.returncode != 0:
                return VirtuosoResult(
                    status=ExecutionStatus.ERROR,
                    errors=[result.stderr.strip() or f"Failed to download {source}"],
                    execution_time=elapsed,
                    metadata={"remote_path": source, "local_path": str(destination)},
                )
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=str(destination),
                execution_time=elapsed,
                metadata={"remote_path": source, "local_path": str(destination)},
            )

        # Local mode: just copy
        import shutil
        try:
            shutil.copy2(Path(source), destination)
        except OSError as exc:
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[f"Failed to copy {source}: {exc}"],
                execution_time=time.perf_counter() - started,
            )
        return VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output=str(destination),
            execution_time=time.perf_counter() - started,
        )

    def upload_file(self, local_path: str | Path, remote_path: str | Path,
                    *, timeout: int | None = None) -> VirtuosoResult:
        started = time.perf_counter()
        source = Path(local_path)
        destination = _path_to_posix(remote_path)

        if not source.exists():
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[f"Local file not found: {source}"],
                execution_time=time.perf_counter() - started,
            )

        if self._tunnel is not None:
            result = self._tunnel.upload_file(source, destination, timeout=timeout)
            elapsed = time.perf_counter() - started
            if result.returncode != 0:
                return VirtuosoResult(
                    status=ExecutionStatus.ERROR,
                    errors=[result.stderr.strip() or f"Failed to upload {source}"],
                    execution_time=elapsed,
                )
            return VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=destination,
                execution_time=elapsed,
            )

        # Local mode
        import shutil
        try:
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError as exc:
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[f"Failed to copy {source}: {exc}"],
                execution_time=time.perf_counter() - started,
            )
        return VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output=str(destination),
            execution_time=time.perf_counter() - started,
        )

    # -- IL loading ---------------------------------------------------------

    def load_il(self, path: str | Path, timeout: int | None = None) -> VirtuosoResult:
        """Load an IL file in Virtuoso."""
        try:
            prepared, uploaded = self._prepare_il_path(path)
        except Exception as e:
            return VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=[f"Failed to prepare IL path: {e}"],
            )

        effective_timeout = timeout if timeout is not None else self._timeout
        skill_command = f'load("{_escape_for_skill_evalstring_source(prepared)}")'
        result = self.execute_skill(skill_command, timeout=effective_timeout)

        if self._log_to_ciw and result.status == ExecutionStatus.SUCCESS:
            self.ciw_log(
                f'printf("[RAMIC] loaded {_escape_skill_string(prepared)}\\n")',
                timeout=5,
            )

        result.metadata["uploaded"] = uploaded
        result.metadata["skill_command"] = skill_command
        return result

    def run_il_file(self, path: str | Path, lib: str, cell: str, *,
                    view: str = "layout", view_type: str | None = None,
                    mode: str = "w", open_window: bool = True,
                    save: bool = False, timeout: int | None = None) -> VirtuosoResult:
        effective_timeout = timeout if timeout is not None else self._timeout
        opened = self.open_cell_view(lib, cell, view=view, view_type=view_type, mode=mode, timeout=effective_timeout)
        if opened.status != ExecutionStatus.SUCCESS:
            return opened
        if open_window:
            window_result = self.open_window(lib, cell, view=view, view_type=view_type, timeout=effective_timeout)
            if window_result.status != ExecutionStatus.SUCCESS:
                return window_result
        sync_result = self.execute_skill("cv = geGetEditCellView()", timeout=effective_timeout)
        if sync_result.status != ExecutionStatus.SUCCESS:
            return sync_result
        load_result = self.load_il(path, timeout=effective_timeout)
        if load_result.status != ExecutionStatus.SUCCESS or not save:
            return load_result
        save_result = self.save_current_cellview(timeout=effective_timeout)
        save_result.metadata["load_result"] = load_result.model_dump(mode="json")
        return save_result

    def execute_operations(self, commands: list[str], *, timeout: int | None = None,
                           wrap_in_progn: bool = True) -> VirtuosoResult:
        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            script = compose_skill_script(commands, wrap_in_progn=wrap_in_progn)
        except ValueError as exc:
            return VirtuosoResult(status=ExecutionStatus.ERROR, errors=[str(exc)])
        result = self.execute_skill(script, timeout=effective_timeout)
        result.metadata.setdefault("operation_count", len([c for c in commands if c and c.strip()]))
        return result

    # -- private helpers ----------------------------------------------------

    def _prepare_il_path(self, path: str | Path) -> tuple[str, bool]:
        """Return (remote_path, uploaded) where uploaded=False means cache hit."""
        p = Path(path)
        if self._tunnel is not None and p.is_file():
            from virtuoso_bridge.transport.remote_paths import default_virtuoso_bridge_dir, resolve_remote_username
            work_dir = self._tunnel.remote_work_dir
            if not work_dir:
                remote_username = resolve_remote_username(
                    configured_user=getattr(self._tunnel, '_remote_user', None),
                    runner=self._tunnel.ssh_runner,
                )
                work_dir = default_virtuoso_bridge_dir(remote_username, "virtuoso_bridge")
            remote_dir = work_dir.rstrip("/")
            remote_path = f"{remote_dir}/{p.name}"
            content = p.read_bytes()
            md5 = hashlib.md5(content).hexdigest()
            cached = self._il_upload_cache.get(str(p))
            if cached and cached[0] == md5:
                return cached[1], False
            up = self._tunnel.upload_text(content.decode("utf-8"), remote_path)
            if up.returncode != 0:
                raise RuntimeError(f"Failed to upload IL file {p.name}: {up.stderr.strip()}")
            remote_posix = _path_to_posix(remote_path)
            self._il_upload_cache[str(p)] = (md5, remote_posix)
            return remote_posix, True
        return _path_to_posix(p), False

    def _execute_skill_once(self, skill_code: str, timeout: int) -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((self._host, self._port))
            payload = json.dumps({"skill": skill_code, "timeout": timeout}).encode("utf-8")
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = s.recv(_RECV_BUF_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="ignore")

    @staticmethod
    def _should_retry_tunnel_connect(exc: OSError, now: float, connect_deadline: float) -> bool:
        if now >= connect_deadline:
            return False
        retryable_errno = {
            getattr(errno, "ECONNREFUSED", 111),
            getattr(errno, "ECONNRESET", 104),
            getattr(errno, "ENOTCONN", 57),
        }
        return getattr(exc, "errno", None) in retryable_errno or "Connection refused" in str(exc)

    @staticmethod
    def _parse_response(raw: str, elapsed: float) -> VirtuosoResult:
        if not raw:
            return VirtuosoResult(status=ExecutionStatus.ERROR, errors=["Empty response from daemon"], execution_time=elapsed)
        if "TimeoutError" in raw:
            return VirtuosoResult(status=ExecutionStatus.ERROR, errors=["SKILL execution timeout in Virtuoso"], execution_time=elapsed)
        if raw.startswith(_STX):
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=raw[1:], execution_time=elapsed)
        if raw.startswith(_NAK):
            return VirtuosoResult(status=ExecutionStatus.ERROR, errors=[raw[1:]], execution_time=elapsed)
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=raw, execution_time=elapsed,
                              warnings=["Response did not contain a standard status marker"])

    # -- cleanup ------------------------------------------------------------

    def close(self) -> None:
        """Close the bridge. Tunnel cleanup is handled by SSHClient."""
        if self._tunnel is not None:
            try:
                self._tunnel.close()
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

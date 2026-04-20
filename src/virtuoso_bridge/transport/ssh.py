"""SSH runner for remote command execution."""

from __future__ import annotations

import atexit
import base64
import binascii
import logging
import os
import queue
import shlex
import shutil
import signal
import tempfile
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, NamedTuple

from virtuoso_bridge.env import load_vb_env

logger = logging.getLogger(__name__)

# ── Command log file ─────────────────────────────────────────────────────
# All SSH/SCP/tunnel commands across all modules are logged to this file.
def _project_log_dir() -> Path:
    """Find project root (pyproject.toml), fall back to ~/.cache/virtuoso_bridge."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent / "logs"
    return Path.home() / ".cache" / "virtuoso_bridge"

_LOG_DIR = _project_log_dir()
_LOG_FILE = _LOG_DIR / "commands.log"

def _setup_command_log() -> None:
    """Add a file handler to the package root logger."""
    pkg_logger = logging.getLogger("virtuoso_bridge")
    if any(getattr(h, '_vb_cmd_log', False) for h in pkg_logger.handlers):
        return
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    fh._vb_cmd_log = True  # type: ignore[attr-defined]
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    pkg_logger.addHandler(fh)
    if pkg_logger.level == logging.NOTSET or pkg_logger.level > logging.DEBUG:
        pkg_logger.setLevel(logging.DEBUG)

_setup_command_log()

_INTERPRETER_SHUTTING_DOWN = False

def _mark_interpreter_shutdown() -> None:
    global _INTERPRETER_SHUTTING_DOWN
    _INTERPRETER_SHUTTING_DOWN = True

atexit.register(_mark_interpreter_shutdown)


def _windows_no_window_kwargs(
    *,
    detached: bool = False,
    new_process_group: bool = False,
) -> dict[str, Any]:
    """Best-effort Windows process flags for CLI tools like ssh/scp/tar."""
    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    startupinfo.wShowWindow = 0  # SW_HIDE
    creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    if detached:
        creationflags |= subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    if new_process_group:
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return {
        "creationflags": creationflags,
        "close_fds": True,
        "startupinfo": startupinfo,
    }

class RemoteSshEnv(NamedTuple):
    """SSH settings read from environment variables."""

    remote_host: str | None
    remote_user: str | None
    jump_host: str | None
    jump_user: str | None

def remote_ssh_env_from_os(profile: str | None = None) -> RemoteSshEnv:
    """Read remote SSH target from environment variables.

    If *profile* is given (e.g. ``"gpu1"``), reads ``VB_REMOTE_HOST_GPU1``
    etc.  Otherwise reads the default unsuffixed variables.
    """
    load_vb_env()
    suffix = f"_{profile}" if profile else ""

    def _strip(name: str) -> str | None:
        raw = os.environ.get(f"{name}{suffix}")
        if raw is None:
            return None
        s = raw.strip()
        return s or None

    return RemoteSshEnv(
        remote_host=_strip("VB_REMOTE_HOST"),
        remote_user=_strip("VB_REMOTE_USER"),
        jump_host=_strip("VB_JUMP_HOST"),
        jump_user=_strip("VB_JUMP_USER"),
    )

class CommandResult(NamedTuple):
    """Result of a remote command execution."""

    returncode: int
    stdout: str
    stderr: str


def _tool_override_from_env(var_name: str) -> str | None:
    raw = os.environ.get(var_name)
    if raw is None:
        return None
    value = os.path.expandvars(os.path.expanduser(raw.strip()))
    return value or None


def _as_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

def _derive_tool(base_cmd: str, old_name: str, new_name: str) -> str:
    """Derive a sibling tool path from a known tool (e.g. ssh -> scp).

    Handles both 'ssh' and 'ssh.exe' endings.
    """
    for suffix in (old_name + ".exe", old_name):
        if base_cmd.endswith(suffix):
            candidate = base_cmd[: -len(suffix)] + new_name + (".exe" if suffix.endswith(".exe") else "")
            if os.path.isfile(candidate):
                return candidate
    return shutil.which(new_name) or new_name


class SSHRunner:
    """Generic SSH/rsync/tar runner using OpenSSH CLI tools."""

    def __init__(
        self,
        host: str,
        user: str | None = None,
        jump_host: str | None = None,
        jump_user: str | None = None,
        ssh_key_path: Path | None = None,
        ssh_config_path: Path | None = None,
        ssh_cmd: str | None = None,
        timeout: int = 600,
        connect_timeout: int = 30,
        persistent_shell: bool = False,
        verbose: bool = False,
    ) -> None:
        load_vb_env()
        self._host = host
        self._user = user
        self._jump_host = jump_host
        self._jump_user = jump_user or user
        self._ssh_key_path = ssh_key_path
        self._ssh_config_path = ssh_config_path
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._persistent_shell_enabled = persistent_shell and os.name != "nt"
        self._verbose = verbose

        env_ssh_cmd = _tool_override_from_env("VB_SSH_CMD")
        env_scp_cmd = _tool_override_from_env("VB_SCP_CMD")
        env_tar_cmd = _tool_override_from_env("VB_TAR_CMD")

        self._ssh_cmd = ssh_cmd or env_ssh_cmd or shutil.which("ssh") or "ssh"
        self._scp_cmd = env_scp_cmd or _derive_tool(self._ssh_cmd, "ssh", "scp")
        self._tar_cmd = env_tar_cmd or shutil.which("tar") or "tar"

        # ControlMaster socket path for SSH connection multiplexing.
        # All ssh/scp calls to the same host reuse one TCP connection.
        # Disabled on Windows: Windows OpenSSH does not support Unix domain
        # sockets required by ControlMaster.  Users can also force-disable
        # via VB_DISABLE_CONTROL_MASTER=1.
        _disable_cm = os.environ.get("VB_DISABLE_CONTROL_MASTER", "").strip().lower() in ("1", "true", "yes")
        _force_cm = os.environ.get("VB_FORCE_CONTROL_MASTER", "").strip().lower() in ("1", "true", "yes")
        self._use_control_master = _force_cm or ((os.name != "nt") and (not _disable_cm))

        _user_part = user or "default"
        _tmp = tempfile.gettempdir()
        self._control_path = f"{_tmp}/vb_ssh_{_user_part}@{host}:{jump_host or 'direct'}"

        if not self._use_control_master:
            logger.debug("ControlMaster disabled (os=%s, env_override=%s)", os.name, _disable_cm)
        if persistent_shell and not self._persistent_shell_enabled:
            logger.debug("Persistent SSH shell disabled on Windows for host %s", host)

        self._shell_proc: subprocess.Popen[Any] | None = None
        self._shell_queue: queue.Queue[str | None] | None = None
        self._shell_reader: threading.Thread | None = None
        self._shell_lock = threading.RLock()

        # Port-forwarding tunnel state
        self._tunnel_proc: subprocess.Popen[Any] | None = None
        self._tunnel_pid: int | None = None
        self._tunnel_using_external = False

    @property
    def host(self) -> str:
        """Target hostname."""
        return self._host

    @property
    def user(self) -> str | None:
        """SSH user name."""
        return self._user

    @property
    def persistent_shell_enabled(self) -> bool:
        """Whether run_command / upload_text reuse one SSH shell."""
        return self._persistent_shell_enabled

    # -- port-forwarding tunnel ----------------------------------------------

    def start_port_forward(self, port: int, settle: float = 1.5, *, remote_port: int | None = None) -> subprocess.Popen[Any] | None:
        """Start a persistent SSH port-forwarding tunnel.

        *port* is the local port to bind.  *remote_port* is the port on the
        remote side; defaults to *port* when not specified.

        Returns the Popen process on success, or None if reusing an existing
        tunnel (port already reachable).  Raises RuntimeError on failure.
        """
        if remote_port is None:
            remote_port = port
        cmd: list[str] = [self._ssh_cmd]
        # Use ControlMaster options — if a master already exists, the slave
        # will request port-forwarding from it and then exit.  The master
        # keeps the forward alive.  If no master exists, this becomes the
        # master (ControlMaster=auto).
        cmd += self._common_ssh_options()
        cmd += [
            "-o", "ExitOnForwardFailure=yes",
            "-N",
            "-L", f"{port}:127.0.0.1:{remote_port}",
        ]
        if self._user:
            cmd.append(f"{self._user}@{self._host}")
        else:
            cmd.append(self._host)

        logger.info("Starting SSH tunnel: %s", " ".join(cmd))
        if self._verbose:
            print(f"[cmd] {' '.join(cmd)}", flush=True)

        if os.name == "nt":
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_windows_no_window_kwargs(detached=True, new_process_group=True),
            )
            jh_settle = max(settle, 3.0) if self._jump_host else settle
            deadline = time.monotonic() + jh_settle
            while time.monotonic() < deadline:
                if self.can_reach_port(port):
                    self._tunnel_proc = proc
                    self._tunnel_pid = proc.pid
                    self._tunnel_using_external = False
                    return proc
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            if self.can_reach_port(port):
                logger.info("Reusing existing tunnel at localhost:%d", port)
                self._tunnel_using_external = True
                return None
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except OSError:
                pass
            except subprocess.TimeoutExpired:
                proc.kill()
            rc = proc.poll()
            detail = f" (rc={rc})" if rc is not None else ""
            raise RuntimeError(f"SSH tunnel failed to start on Windows{detail}")

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "start_new_session": True,
            "stderr": subprocess.PIPE,
        }
        proc = subprocess.Popen(cmd, **popen_kwargs)

        jh_settle = max(settle, 3.0) if self._jump_host else settle
        deadline = time.monotonic() + jh_settle
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)

        if proc.poll() is not None:
            err_msg = ""
            if proc.stderr and proc.stderr.readable():
                try:
                    err_msg = proc.stderr.read().decode("utf-8", errors="ignore")
                except (OSError, ValueError):
                    pass
            # Slave exited — check if ControlMaster took over the forward
            if self.can_reach_port(port):
                logger.info("Port forward active at localhost:%d (ControlMaster)", port)
                self._tunnel_using_external = True
                return None
            if "address already in use" in err_msg.lower():
                logger.info("Reusing existing tunnel at localhost:%d", port)
                self._tunnel_using_external = True
                return None
            return proc  # failed — caller inspects poll/stderr
        self._tunnel_proc = proc
        self._tunnel_pid = proc.pid
        return proc  # running

    def stop_port_forward(self) -> None:
        """Stop the port-forwarding tunnel.

        If ControlMaster is managing the forward, use ``ssh -O exit`` to
        cleanly shut it down.  Falls back to SIGTERM on a standalone tunnel
        process.
        """
        # Try ControlMaster exit first
        if self._use_control_master and Path(self._control_path).exists():
            cmd = [self._ssh_cmd, "-o", f"ControlPath={self._control_path}", "-O", "exit"]
            if self._user:
                cmd.append(f"{self._user}@{self._host}")
            else:
                cmd.append(self._host)
            try:
                subprocess.run(cmd, capture_output=True, timeout=5)
                logger.info("ControlMaster exited via -O exit")
            except (subprocess.TimeoutExpired, OSError):
                pass

        # Fallback: kill by PID
        pid = None
        if self._tunnel_proc is not None and self._tunnel_proc.poll() is None:
            pid = self._tunnel_proc.pid
        elif self._tunnel_pid:
            pid = self._tunnel_pid
        if pid:
            logger.info("Terminating SSH tunnel (PID %d)", pid)
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, PermissionError):
                pass
        self._tunnel_proc = None
        self._tunnel_pid = None
        self._tunnel_using_external = False

    @property
    def is_tunnel_alive(self) -> bool:
        if self._tunnel_proc is not None and self._tunnel_proc.poll() is None:
            return True
        if self._tunnel_using_external and self._tunnel_pid:
            try:
                os.kill(self._tunnel_pid, 0)
                return True
            except (OSError, PermissionError):
                pass
        return False

    @property
    def tunnel_pid(self) -> int | None:
        if self._tunnel_proc is not None and self._tunnel_proc.poll() is None:
            return self._tunnel_proc.pid
        return self._tunnel_pid

    @tunnel_pid.setter
    def tunnel_pid(self, value: int | None) -> None:
        self._tunnel_pid = value
        if value is not None:
            self._tunnel_using_external = True

    @staticmethod
    def can_reach_port(port: int) -> bool:
        """Check if localhost:port accepts TCP connections."""
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.close()
            return True
        except (ConnectionRefusedError, OSError):
            return False

    # -- connection test -------------------------------------------------------

    def test_connection(self, timeout: int | None = None) -> bool:
        """Test SSH connectivity to the remote host."""
        effective_timeout = timeout or self._connect_timeout
        cmd = self._build_ssh_base() + ["-T", "exit", "0"]
        logger.debug("Testing SSH connection: %s", cmd)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                **_windows_no_window_kwargs(),
            )
            success = result.returncode == 0
            if success:
                logger.info("SSH connection to %s succeeded", self._host)
            else:
                summarized = self._summarize_ssh_transport_error(result.stderr)
                logger.warning(
                    "SSH connection to %s failed: returncode=%d stderr=%s",
                    self._host,
                    result.returncode,
                    summarized,
                )
            return success
        except subprocess.TimeoutExpired:
            logger.warning("SSH connection to %s timed out after %ds", self._host, effective_timeout)
            return False
        except FileNotFoundError:
            logger.error("SSH executable not found: %s", self._ssh_cmd)
            return False
        except OSError as exc:
            logger.error("SSH connection error: %s", exc)
            return False

    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        """Execute a command on the remote host via SSH."""
        if self._persistent_shell_enabled:
            try:
                return self._run_via_persistent_shell_with_retry(command, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log_persistent_shell_fallback("Persistent SSH shell failed", exc)

        return self._run_command_once(command, timeout=timeout)

    def _print_cmd(self, cmd: list[str]) -> None:
        logger.info("[local] %s", " ".join(cmd))
        if self._verbose:
            print(f"[cmd] {' '.join(cmd)}", flush=True)

    def _run_command_once(self, command: str, timeout: int | None = None) -> CommandResult:
        effective_timeout = timeout or self._timeout
        # Pipe the command to `ssh host sh` via stdin so it always runs in sh
        # regardless of the remote user's login shell (which may be csh).
        # Passing the command as an SSH argument would have the login shell
        # interpret it, breaking sh syntax (&&, ${VAR:-}, etc.) if login=csh.
        cmd = self._build_ssh_base() + ["sh"]
        self._print_cmd(cmd)
        logger.info("[server] %s", command)
        # Use bytes (text=False) to bypass Windows universal-newlines translation
        # of '\n' → '\r\n' on stdin. Heredoc payloads (cat > file << EOF ...)
        # otherwise land on the remote with CRLF line endings, which csh reads
        # as part of the next token (e.g. `source /path/to/cshrc\r` → file not
        # found). On POSIX the behavior is identical to text mode.
        result = subprocess.run(
            cmd,
            input=command.encode("utf-8"),
            capture_output=True,
            text=False,
            timeout=effective_timeout,
            **_windows_no_window_kwargs(),
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        logger.debug(
            "Remote command returned %d (stdout=%d bytes, stderr=%d bytes)",
            result.returncode,
            len(stdout),
            len(stderr),
        )
        return CommandResult(returncode=result.returncode, stdout=stdout, stderr=stderr)

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        recursive: bool = False,
        timeout: int | None = None,
    ) -> CommandResult:
        """Upload a file or directory to the remote host via tar pipe."""
        if not local_path.exists():
            raise FileNotFoundError(f"Local path not found: {local_path}")

        effective_timeout = timeout or self._timeout
        result = self._upload_via_tar(local_path, remote_path, timeout=effective_timeout)
        if result.returncode != 0:
            logger.warning("tar upload failed (rc=%d): %s", result.returncode, result.stderr.strip())
        else:
            logger.debug("Upload completed successfully")
        return result

    def upload_batch(
        self,
        files: list[tuple[Path, str]],
        timeout: int | None = None,
    ) -> CommandResult:
        """Upload multiple files in a single tar pipe (all to the same remote dir)."""
        if not files:
            return CommandResult(returncode=0, stdout="", stderr="")

        effective_timeout = timeout or self._timeout

        # Group by remote directory (usually all the same)
        by_remote_dir: dict[str, list[tuple[Path, str]]] = {}
        for local_path, remote_path in files:
            rdir = str(Path(remote_path).parent).replace("\\", "/")
            by_remote_dir.setdefault(rdir, []).append((local_path, remote_path))

        for remote_dir, entries in by_remote_dir.items():
            remote_dir_q = shlex.quote(remote_dir)
            remote_cmd = f"mkdir -p {remote_dir_q} && tar xf - -C {remote_dir_q}"
            ssh_cmd = self._build_ssh_base() + [remote_cmd]

            tar_cmd = [self._tar_cmd, "cf", "-"]
            for local_path, _ in entries:
                tar_cmd += ["-C", str(local_path.resolve().parent).replace("\\", "/"), local_path.name]

            logger.debug("Batch tar upload: %d file(s) -> %s:%s", len(entries), self._host, remote_dir)
            tar_proc = subprocess.Popen(
                tar_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_windows_no_window_kwargs(),
            )
            ssh_proc = subprocess.Popen(
                ssh_cmd,
                stdin=tar_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_windows_no_window_kwargs(),
            )
            if tar_proc.stdout:
                tar_proc.stdout.close()
            try:
                ssh_out, ssh_err = ssh_proc.communicate(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                ssh_proc.kill()
                tar_proc.kill()
                raise
            tar_proc.wait()

            if ssh_proc.returncode != 0:
                stderr_text = _as_text(ssh_err)
                logger.warning("tar batch upload failed (rc=%d): %s", ssh_proc.returncode, stderr_text.strip())
                return CommandResult(
                    returncode=ssh_proc.returncode,
                    stdout=_as_text(ssh_out),
                    stderr=stderr_text,
                )

        return CommandResult(returncode=0, stdout="", stderr="")

    def upload_text(self, text: str, remote_path: str, timeout: int | None = None) -> CommandResult:
        """Upload a UTF-8 text string as a file to the remote host via SSH."""
        if self._persistent_shell_enabled:
            if not text.endswith("\n"):
                text = text + "\n"
            remote_dir = str(Path(remote_path).parent).replace("\\", "/")
            quoted_dir = shlex.quote(remote_dir)
            quoted_path = shlex.quote(remote_path.replace("\\", "/"))
            payload_token = f"__vb_PAYLOAD_{uuid.uuid4().hex}__"
            command = (
                f"mkdir -p {quoted_dir} && chmod 755 {quoted_dir}\n"
                f"cat > {quoted_path} <<'{payload_token}'\n"
                f"{text}"
                f"{payload_token}\n"
            )
            try:
                return self._run_via_persistent_shell_with_retry(command, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log_persistent_shell_fallback("Persistent SSH text upload failed", exc)

        effective_timeout = timeout or self._timeout
        remote_dir = str(Path(remote_path).parent).replace("\\", "/")
        quoted_dir = shlex.quote(remote_dir)
        quoted_path = shlex.quote(remote_path.replace("\\", "/"))
        remote_cmd = (
            "sh -lc "
            + shlex.quote(
                f"mkdir -p {quoted_dir} && chmod 755 {quoted_dir} && cat > {quoted_path}"
            )
        )
        cmd = self._build_ssh_base() + [remote_cmd]
        if self._verbose:
            print(f"[cmd] {' '.join(cmd)}  # upload -> {remote_path}", flush=True)
        logger.debug("Uploading text payload (%d chars) -> %s:%s", len(text), self._host, remote_path)
        result = subprocess.run(
            cmd,
            input=text,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            **_windows_no_window_kwargs(),
        )
        if result.returncode != 0:
            logger.warning("SSH text upload failed (rc=%d): %s", result.returncode, result.stderr.strip())
        else:
            logger.debug("Text upload completed successfully")
        return CommandResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def download(
        self,
        remote_path: str,
        local_path: Path,
        recursive: bool = False,
        timeout: int | None = None,
    ) -> CommandResult:
        """Download a file or directory from the remote host via tar pipe or scp."""
        effective_timeout = timeout or self._timeout

        if recursive:
            return self._download_via_tar(remote_path, local_path, timeout=effective_timeout)

        local_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self._scp_cmd] + self._common_ssh_options()
        cmd += [self._remote_scp_target(remote_path), str(local_path)]
        self._print_cmd(cmd)
        logger.debug("Downloading via scp %s:%s -> %s", self._host, remote_path, local_path)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            **_windows_no_window_kwargs(),
        )
        if result.returncode != 0:
            logger.warning("download (scp) failed (rc=%d): %s", result.returncode, result.stderr.strip())
        else:
            logger.debug("Download completed successfully")
        return CommandResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def _download_via_tar(
        self,
        remote_path: str,
        local_path: Path,
        *,
        timeout: int,
    ) -> CommandResult:
        """Download a directory recursively using tar czf piped over SSH."""
        # Ensure the parent of the local target exists (like scp -r does)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # To match scp -r behavior, we compress the *directory itself* (not its contents)
        # and extract it into local_path.parent. If local_path.name != remote basename,
        # we will extract it then rename it.
        remote_path_q = shlex.quote(remote_path)
        remote_parent = f"$(dirname {remote_path_q})"
        remote_base = f"$(basename {remote_path_q})"
        inner_cmd = f"cd {remote_parent} && tar czf - {remote_base}"
        remote_cmd = f"sh -c {shlex.quote(inner_cmd)}"

        ssh_cmd = self._build_ssh_base() + [remote_cmd]
        tar_cmd = [self._tar_cmd, "xzf", "-", "-C", str(local_path.parent).replace("\\", "/")]

        if self._verbose:
            print(f"[cmd] {' '.join(ssh_cmd)} | {' '.join(tar_cmd)}  # download {remote_path} -> {local_path}", flush=True)
        logger.debug("Downloading via tar pipe %s:%s -> %s", self._host, remote_path, local_path)

        ssh_proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_windows_no_window_kwargs(),
        )
        tar_proc = subprocess.Popen(
            tar_cmd,
            stdin=ssh_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_windows_no_window_kwargs(),
        )
        if ssh_proc.stdout:
            ssh_proc.stdout.close()

        try:
            tar_out, tar_err = tar_proc.communicate(timeout=timeout)
            ssh_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ssh_proc.kill()
            tar_proc.kill()
            raise

        if ssh_proc.returncode != 0 or tar_proc.returncode != 0:
            ssh_err = _as_text(ssh_proc.stderr.read()) if ssh_proc.stderr else ""
            tar_err_str = _as_text(tar_err)
            combined_err = f"SSH error: {ssh_err.strip()} | Tar error: {tar_err_str.strip()}"
            logger.warning("download (tar) failed (rc=%d/%d): %s", ssh_proc.returncode, tar_proc.returncode, combined_err)
            return CommandResult(returncode=ssh_proc.returncode or tar_proc.returncode, stdout="", stderr=combined_err)

        # Handle rename if the remote basename differs from local_path.name
        remote_basename = Path(remote_path).name
        if remote_basename != local_path.name:
            extracted_path = local_path.parent / remote_basename
            if extracted_path.exists():
                if local_path.exists():
                    shutil.rmtree(local_path)
                extracted_path.rename(local_path)

        return CommandResult(returncode=0, stdout="", stderr="")

    def _upload_via_tar(
        self,
        local_path: Path,
        remote_path: str,
        *,
        timeout: int,
    ) -> CommandResult:
        remote_dir = str(Path(remote_path).parent).replace("\\", "/")
        remote_dir_q = shlex.quote(remote_dir)
        remote_cmd = f"mkdir -p {remote_dir_q} && tar xf - -C {remote_dir_q}"
        ssh_cmd = self._build_ssh_base() + [remote_cmd]
        tar_cmd = [self._tar_cmd, "cf", "-", "-C", str(local_path.parent).replace("\\", "/"), local_path.name]
        if self._verbose:
            print(f"[cmd] {' '.join(tar_cmd)} | {' '.join(ssh_cmd)}  # upload {local_path} -> {remote_path}", flush=True)
        logger.debug("Uploading via tar pipe %s -> %s:%s", local_path, self._host, remote_path)
        tar_proc = subprocess.Popen(
            tar_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_windows_no_window_kwargs(),
        )
        ssh_proc = subprocess.Popen(
            ssh_cmd,
            stdin=tar_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_windows_no_window_kwargs(),
        )
        if tar_proc.stdout:
            tar_proc.stdout.close()
        try:
            ssh_out, ssh_err = ssh_proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            ssh_proc.kill()
            tar_proc.kill()
            raise
        tar_proc.wait()
        return CommandResult(
            returncode=ssh_proc.returncode,
            stdout=_as_text(ssh_out),
            stderr=_as_text(ssh_err),
        )

    def ensure_persistent_shell(self, timeout: int | None = None) -> None:
        """Start the reusable SSH shell on first use."""
        if not self._persistent_shell_enabled:
            return

        with self._shell_lock:
            if self._shell_proc is not None and self._shell_proc.poll() is None:
                return

            self._close_persistent_shell_locked()
            cmd = self._build_ssh_base() + ["sh", "-s"]
            logger.info("Starting persistent SSH shell: %s", " ".join(cmd))
            self._print_cmd(cmd)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                **_windows_no_window_kwargs(),
            )
            if proc.stdin is None or proc.stdout is None:
                proc.terminate()
                raise RuntimeError("Failed to allocate pipes for persistent SSH shell.")

            self._shell_proc = proc
            self._shell_queue = queue.Queue()
            self._shell_reader = threading.Thread(
                target=self._pump_shell_output,
                args=(proc.stdout, self._shell_queue),
                daemon=True,
                name=f"ssh-shell-{self._host}",
            )
            self._shell_reader.start()

            probe_timeout = timeout or self._connect_timeout
            try:
                probe = self._run_command_via_persistent_shell_locked(":", probe_timeout)
            except Exception:
                self._close_persistent_shell_locked()
                raise

            if probe.returncode != 0:
                self._close_persistent_shell_locked()
                details = self._summarize_ssh_transport_error(probe.stderr.strip() or probe.stdout.strip())
                raise RuntimeError(
                    f"Persistent SSH shell probe failed: {details}"
                )

    def close(self) -> None:
        """Release any persistent SSH resources held by this runner."""
        with self._shell_lock:
            self._close_persistent_shell_locked()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def _log_persistent_shell_fallback(self, message: str, exc: Exception) -> None:
        """Log a fallback from the persistent shell at the right severity."""
        if _INTERPRETER_SHUTTING_DOWN or "interpreter shutdown" in str(exc).lower():
            logger.debug("%s for %s; falling back to one-shot SSH: %s", message, self._host, exc)
            return
        logger.warning("%s for %s; falling back to one-shot SSH: %s", message, self._host, exc)

    def describe_ssh_command_failure(self, action: str, result: CommandResult) -> str:
        """Format an SSH/SCP failure without leaking low-level transport noise."""
        details = self._summarize_ssh_transport_error(result.stderr or result.stdout)
        if details:
            return f"Failed to {action}: {details}"
        return f"Failed to {action}: SSH command exited with code {result.returncode}."

    def _summarize_ssh_transport_error(self, raw_message: str | None) -> str:
        text = " ".join((raw_message or "").split())
        if not text:
            return f"SSH connection to {self._host} failed."

        lower = text.lower()
        if "could not resolve hostname" in lower:
            return (
                f"SSH host lookup failed for {self._host}. "
                "Check ~/.ssh/config, VB_REMOTE_HOST, and VB_JUMP_HOST."
            )
        if "permission denied" in lower:
            return (
                f"SSH authentication failed for {self._host}. "
                "Check your SSH key, username, and remote access permissions."
            )
        if "connection timed out" in lower or "operation timed out" in lower or "no route to host" in lower:
            return (
                f"SSH connection to {self._host} timed out. "
                "Check network access, VPN, and SSH reachability."
            )
        if "connection refused" in lower and "port 22" in lower:
            return f"SSH server on {self._host} refused the connection."
        if (
            "unknown port 65535" in lower
            or "kex_exchange_identification" in lower
            or "connection closed by" in lower
        ):
            if self._jump_host:
                return (
                    f"SSH connection to {self._host} was closed before login. "
                    f"Check the jump host {self._jump_host} and the target host SSH path."
                )
            return (
                f"SSH connection to {self._host} was closed before login. "
                "Check that the host is reachable and your SSH config is correct."
            )
        return text

    @staticmethod
    def _is_retryable_persistent_shell_error(exc: Exception) -> bool:
        message = str(exc).lower()
        retryable_fragments = (
            "invalid base64 payload",
            "unexpected persistent shell protocol line",
            "unexpected persistent shell return line",
            "persistent ssh shell exited unexpectedly",
            "failed to write to persistent ssh shell",
        )
        return any(fragment in message for fragment in retryable_fragments)

    def _run_via_persistent_shell_with_retry(
        self,
        command: str,
        timeout: int | None = None,
    ) -> CommandResult:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                with self._shell_lock:
                    self.ensure_persistent_shell(timeout=timeout)
                    return self._run_command_via_persistent_shell_locked(command, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                with self._shell_lock:
                    self._close_persistent_shell_locked()
                if attempt == 0 and self._is_retryable_persistent_shell_error(exc):
                    logger.info(
                        "Retrying persistent SSH shell for %s after recoverable protocol error: %s",
                        self._host,
                        exc,
                    )
                    continue
                raise

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Persistent SSH shell retry path failed without an exception.")

    @staticmethod
    def _pump_shell_output(stream, out_queue: queue.Queue[str | None]) -> None:
        try:
            for line in stream:
                out_queue.put(line.decode("utf-8", errors="replace"))
        finally:
            out_queue.put(None)

    def _run_command_via_persistent_shell_locked(
        self, command: str, timeout: int | None = None
    ) -> CommandResult:
        proc = self._shell_proc
        out_queue = self._shell_queue
        if proc is None or proc.stdin is None or proc.poll() is not None or out_queue is None:
            raise RuntimeError("Persistent SSH shell is not running.")

        logger.info("[server] %s", command)
        if self._verbose:
            # Show a compact summary: first non-empty, non-mkdir, non-probe line
            lines = [l.strip() for l in command.splitlines() if l.strip()]
            summary = next(
                (l for l in lines if not l.startswith("mkdir ") and l not in (":", "{", "}")),
                None,
            )
            if summary is None:
                pass  # suppress pure probe/mkdir-only commands
            else:
                # Trim heredoc payload: cat > path <<'TOKEN' → cat > path
                if "<<'" in summary:
                    summary = summary.split("<<'")[0].rstrip()
                print(f"[cmd] {self._host}: {summary}", flush=True)
        token = uuid.uuid4().hex
        begin_marker = f"__vb_STDOUT_B64_BEGIN_{token}__"
        stderr_marker = f"__vb_STDERR_B64_BEGIN_{token}__"
        rc_prefix = f"__vb_RC_{token}__"
        script = (
            "__vb_stdout=$(mktemp)\n"
            "__vb_stderr=$(mktemp)\n"
            "{\n"
            f"{command}\n"
            "} >\"$__vb_stdout\" 2>\"$__vb_stderr\"\n"
            "__vb_rc=$?\n"
            f"printf '%s\\n' '{begin_marker}'\n"
            "base64 <\"$__vb_stdout\" | tr -d '\\n'\n"
            f"printf '\\n%s\\n' '{stderr_marker}'\n"
            "base64 <\"$__vb_stderr\" | tr -d '\\n'\n"
            f"printf '\\n{rc_prefix}%s\\n' \"$__vb_rc\"\n"
            "rm -f \"$__vb_stdout\" \"$__vb_stderr\"\n"
        )

        try:
            proc.stdin.write(script.encode("utf-8"))
            proc.stdin.flush()
        except OSError as exc:
            raise RuntimeError(f"Failed to write to persistent SSH shell: {exc}") from exc

        effective_timeout = timeout or self._timeout
        deadline = time.monotonic() + effective_timeout
        stdout_b64 = None
        stderr_b64 = None
        rc = None
        phase = "scan"
        preamble: list[str] = []

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._close_persistent_shell_locked()
                raise subprocess.TimeoutExpired(cmd=command, timeout=effective_timeout)
            try:
                line = out_queue.get(timeout=remaining)
            except queue.Empty as exc:
                self._close_persistent_shell_locked()
                raise subprocess.TimeoutExpired(cmd=command, timeout=effective_timeout) from exc

            if line is None:
                self._close_persistent_shell_locked()
                raise RuntimeError("Persistent SSH shell exited unexpectedly.")

            stripped = line.rstrip("\r\n")
            if phase == "scan":
                if stripped == begin_marker:
                    phase = "stdout_b64"
                elif stripped:
                    preamble.append(stripped)
                continue
            if phase == "stdout_b64":
                stdout_b64 = stripped
                phase = "expect_stderr_marker"
                continue
            if phase == "expect_stderr_marker":
                if stripped == "":
                    continue
                if stripped != stderr_marker:
                    raise RuntimeError(f"Unexpected persistent shell protocol line: {stripped!r}")
                phase = "stderr_b64"
                continue
            if phase == "stderr_b64":
                stderr_b64 = stripped
                phase = "expect_rc"
                continue
            if phase == "expect_rc":
                if stripped == "":
                    continue
                if stripped.startswith(rc_prefix):
                    rc = int(stripped[len(rc_prefix):])
                    break
                raise RuntimeError(f"Unexpected persistent shell return line: {stripped!r}")

        if preamble:
            logger.debug("Ignoring %d preamble line(s) from persistent SSH shell to %s", len(preamble), self._host)

        stdout = self._decode_b64_text(stdout_b64)
        stderr = self._decode_b64_text(stderr_b64)
        return CommandResult(returncode=rc, stdout=stdout, stderr=stderr)

    @staticmethod
    def _decode_b64_text(payload: str | None) -> str:
        if not payload:
            return ""
        compact = "".join(payload.split())
        padded = compact + ("=" * (-len(compact) % 4))
        try:
            return base64.b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(f"Persistent SSH shell returned invalid base64 payload: {exc}") from exc

    def _close_persistent_shell_locked(self) -> None:
        proc = self._shell_proc
        reader = self._shell_reader
        if proc is not None and proc.poll() is None:
            logger.info("Terminating persistent SSH shell for %s", self._host)
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if reader is not None and reader.is_alive():
            reader.join(timeout=1)
        self._shell_proc = None
        self._shell_queue = None
        self._shell_reader = None

    def _common_ssh_options(self) -> list[str]:
        """SSH options shared by both ssh and scp commands."""
        opts: list[str] = [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"ConnectTimeout={self._connect_timeout}",
        ]
        if self._use_control_master:
            opts += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_path}",
                "-o", "ControlPersist=3600",
            ]
        if self._ssh_config_path:
            opts += ["-F", str(self._ssh_config_path)]
        if self._ssh_key_path:
            opts += ["-i", str(self._ssh_key_path)]
        if self._jump_host:
            jump_target = (
                f"{self._jump_user}@{self._jump_host}"
                if self._jump_user
                else self._jump_host
            )
            opts += ["-J", jump_target]
        return opts

    def _build_ssh_base(self) -> list[str]:
        cmd: list[str] = [self._ssh_cmd]
        cmd += self._common_ssh_options()
        if self._user:
            cmd += [f"{self._user}@{self._host}"]
        else:
            cmd += [self._host]
        return cmd

    def _remote_scp_target(self, remote_path: str) -> str:
        if self._user:
            return f"{self._user}@{self._host}:{remote_path}"
        return f"{self._host}:{remote_path}"

class RemoteTaskResult(NamedTuple):
    """Result of a generic remote task (upload + run + optional cleanup)."""

    success: bool
    returncode: int
    stdout: str
    stderr: str
    remote_dir: str | None
    error: str | None
    timings: dict[str, float]

def run_remote_task(
    runner: SSHRunner,
    *,
    work_dir_base: str,
    run_id: str,
    uploads: list[tuple[Path, str]],
    command: str,
    timeout: int = 600,
) -> RemoteTaskResult:
    """Run a remote task: upload files, execute command."""
    timings: dict[str, float] = {}
    remote_dir = f"{work_dir_base}/{run_id}"

    for local_path, _ in uploads:
        if not local_path.exists():
            return RemoteTaskResult(
                success=False, returncode=-1, stdout="", stderr="",
                remote_dir=remote_dir, error=f"Local file not found for upload: {local_path}",
                timings=timings,
            )

    started = time.perf_counter()
    upload_result = runner.upload_batch(uploads)
    timings["upload_total"] = time.perf_counter() - started
    if upload_result.returncode != 0:
        return RemoteTaskResult(
            success=False, returncode=-1, stdout=upload_result.stdout,
            stderr=upload_result.stderr, remote_dir=remote_dir,
            error=f"Failed to upload files: {upload_result.stderr.strip()}",
            timings=timings,
        )
    try:
        started = time.perf_counter()
        exec_result = runner.run_command(command, timeout=timeout)
        timings["remote_exec"] = time.perf_counter() - started
    except subprocess.TimeoutExpired:
        return RemoteTaskResult(
            success=False, returncode=-1, stdout="", stderr="",
            remote_dir=remote_dir, error=f"Remote command timed out after {timeout} seconds",
            timings=timings,
        )
    except (FileNotFoundError, OSError) as exc:
        return RemoteTaskResult(
            success=False, returncode=-1, stdout="", stderr="",
            remote_dir=remote_dir, error=f"SSH execution error: {exc}",
            timings=timings,
        )
    return RemoteTaskResult(
        success=True, returncode=exec_result.returncode,
        stdout=exec_result.stdout, stderr=exec_result.stderr,
        remote_dir=remote_dir, error=None,
        timings=timings,
    )

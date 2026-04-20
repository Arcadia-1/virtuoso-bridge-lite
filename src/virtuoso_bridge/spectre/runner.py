"""Cadence Spectre simulator adapter."""

from __future__ import annotations

import importlib.resources
import logging
import os
import shlex
import shutil
import subprocess
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

from virtuoso_bridge.env import load_vb_env
from virtuoso_bridge.models import ExecutionStatus, SimulationResult
from virtuoso_bridge.spectre.parsers import parse_psf_ascii_directory
from virtuoso_bridge.transport.tunnel import _is_localhost
from virtuoso_bridge.transport.remote_paths import (
    default_remote_spectre_work_dir,
    resolve_remote_username,
)
from virtuoso_bridge.transport.ssh import SSHRunner, RemoteTaskResult, run_remote_task, remote_ssh_env_from_os

logger = logging.getLogger(__name__)


SPECTRE_MODE_ARGS: dict[str, list[str]] = {
    "spectre": [],
    "aps": ["+aps"],
    "x": ["+x"],
    "cx": ["+preset=cx", "+mt"],
    "ax": ["+preset=ax", "+mt"],
    "mx": ["+preset=mx", "+mt"],
    "lx": ["+preset=lx", "+mt"],
    "vx": ["+preset=vx", "+mt"],
}

# ---------------------------------------------------------------------------
# Internal run result (not public API)
# ---------------------------------------------------------------------------

class _SpectreRunResult(NamedTuple):
    """Raw execution result before PSF parsing and SimulationResult assembly."""

    success: bool
    output_dir: Path | None
    returncode: int
    stdout: str
    stderr: str
    error: str | None
    metadata: dict[str, Any]

# ---------------------------------------------------------------------------
# Resource helpers
# ---------------------------------------------------------------------------

def _resolve_spectre_invocation(
    spectre_cmd: str,
) -> tuple[str, list[str]]:
    """Split a spectre command string into executable + prefix args (e.g. 'eda spectre' → ('/edadk/bin/eda', ['spectre']))."""
    parts = shlex.split(spectre_cmd) if spectre_cmd.strip() else ["spectre"]
    return parts[0], parts[1:]

def spectre_mode_args(mode: str) -> list[str]:
    """Return standard Spectre CLI arguments for a named simulation mode."""
    key = mode.strip().lower()
    if key not in SPECTRE_MODE_ARGS:
        supported = ", ".join(sorted(SPECTRE_MODE_ARGS))
        raise ValueError(f"Unsupported Spectre mode '{mode}'. Supported: {supported}")
    return list(SPECTRE_MODE_ARGS[key])

def _build_spectre_argv(
    *,
    spectre_cmd: str,
    spectre_args: list[str] | tuple[str, ...] | None,
    output_format: str | None,
    netlist_path: str,
    raw_dir: str | None = None,
    log_file: str | None = None,
) -> list[str]:
    """Construct a fuller Spectre argv similar to direct production runs."""
    spectre_bin, cmd_prefix_args = _resolve_spectre_invocation(spectre_cmd)
    mode_args = [str(a) for a in (spectre_args or []) if str(a).strip()]
    all_flags = cmd_prefix_args + mode_args
    argv = [spectre_bin]
    # Insert subcommand/wrapper prefix args right after binary (e.g. 'eda spectre')
    argv.extend(cmd_prefix_args)
    if "-64" not in all_flags and "-32" not in all_flags:
        argv.append("-64")
    argv.append(netlist_path)
    queue_args = [] if "+lqtimeout" in all_flags else ["+lqtimeout", "900"]
    warning_args = [] if "-maxw" in all_flags else ["-maxw", "5"]
    notice_args = [] if "-maxn" in all_flags else ["-maxn", "5"]
    argv.append("+escchars")
    if log_file:
        argv.extend(["+log", log_file])
    if output_format:
        argv.extend(["-format", output_format])
    if raw_dir:
        argv.extend(["-raw", raw_dir])
    argv.extend(mode_args)
    argv.extend(queue_args)
    argv.extend(warning_args)
    argv.extend(notice_args)
    argv.append("+logstatus")
    return argv


# ---------------------------------------------------------------------------
# Local execution
# ---------------------------------------------------------------------------

def _run_spectre_local(
    *,
    netlist: Path,
    spectre_cmd: str = "spectre",
    spectre_args: list[str] | tuple[str, ...] | None = None,
    timeout: int = 600,
    work_dir: Path | None = None,
    output_format: str | None = "psfascii",
) -> _SpectreRunResult:
    """Run Spectre as a local subprocess."""
    cwd = work_dir or netlist.parent
    raw_dir = str((Path(cwd) / f"{netlist.stem}.raw").resolve())
    log_file = str((Path(cwd) / f"{netlist.stem}.log").resolve())
    cmd = _build_spectre_argv(
        spectre_cmd=spectre_cmd,
        spectre_args=spectre_args,
        output_format=output_format,
        netlist_path=str(netlist),
        raw_dir=raw_dir,
        log_file=log_file,
    )
    spectre_command = " ".join(shlex.quote(part) for part in cmd)
    logger.debug("Running Spectre locally: %s (cwd=%s)", spectre_command, cwd)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
        )
        base_dir = Path(cwd)
        output_dir = base_dir / f"{netlist.stem}.raw"
        if not output_dir.exists():
            output_dir = base_dir / f"{netlist.stem}.psf"
        return _SpectreRunResult(
            success=True,
            output_dir=output_dir if output_dir.exists() else base_dir,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            error=None,
            metadata={"command": spectre_command, "spectre_command": spectre_command},
        )
    except FileNotFoundError:
        return _SpectreRunResult(
            success=False, output_dir=None, returncode=-1,
            stdout="", stderr="",
            error=f"Spectre executable not found: {spectre_cmd}",
            metadata={"command": spectre_command, "spectre_command": spectre_command},
        )
    except subprocess.TimeoutExpired:
        return _SpectreRunResult(
            success=False, output_dir=None, returncode=-1,
            stdout="", stderr="",
            error=f"Spectre simulation timed out after {timeout} seconds",
            metadata={"command": spectre_command, "spectre_command": spectre_command},
        )
    except OSError as exc:
        return _SpectreRunResult(
            success=False, output_dir=None, returncode=-1,
            stdout="", stderr="",
            error=f"OS error running Spectre: {exc}",
            metadata={"command": spectre_command, "spectre_command": spectre_command},
        )

# ---------------------------------------------------------------------------
# Remote execution
# ---------------------------------------------------------------------------

def _run_spectre_remote(
    *,
    netlist: Path,
    params: dict,
    runner: SSHRunner,
    remote_work_dir: str,
    base_output_dir: Path,
    spectre_cmd: str = "spectre",
    spectre_args: list[str] | tuple[str, ...] | None = None,
    output_format: str | None = "psfascii",
    timeout: int = 600,
    keep_remote_files: bool = False,
) -> _SpectreRunResult:
    """Run Spectre on a remote host: upload netlist, run, download results."""
    run_id = uuid.uuid4().hex[:8]
    remote_dir = f"{remote_work_dir}/{run_id}"

    uploads: list[tuple[Path, str]] = []
    uploads.append((netlist, f"{remote_dir}/{netlist.name}"))
    for inc_file in params.get("include_files", []):
        inc_path = Path(inc_file).resolve()
        if inc_path.exists():
            uploads.append((inc_path, f"{remote_dir}/{inc_path.name}"))

    remote_raw_dir = f"{remote_dir}/{netlist.stem}.raw"
    remote_log_file = f"{remote_dir}/spectre.out"
    spectre_argv = _build_spectre_argv(
        spectre_cmd=spectre_cmd,
        spectre_args=list(spectre_args or []) + list(params.get("spectre_args", [])),
        output_format=output_format,
        netlist_path=f"{remote_dir}/{netlist.name}",
        raw_dir=remote_raw_dir,
        log_file=remote_log_file,
    )
    spectre_command = " ".join(shlex.quote(arg) for arg in spectre_argv)
    logger.info("[remote] %s", spectre_command)
    print(f"[Command] {spectre_command}")
    print("[Exec] Remote simulation running...")

    # Source Cadence/Mentor cshrc in csh, then exec spectre — one command, no wrapper file
    _cadence_val = os.environ.get("VB_CADENCE_CSHRC", "").strip()
    _mentor_val = os.environ.get("VB_MENTOR_CSHRC", "").strip()
    source_lines: list[str] = []
    for cshrc in (_cadence_val, _mentor_val):
        if cshrc:
            source_lines.append(f"source {shlex.quote(cshrc)}")
    csh_body = "; ".join(source_lines) if source_lines else ":"
    # Export HOSTNAME & LD_LIBRARY_PATH so csh inherits them (.cshrc may reference $HOSTNAME)
    env_setup = (
        'HOSTNAME=`hostname 2>/dev/null || echo localhost`; export HOSTNAME && '
        'export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" && '
    )
    pid_file = f"{remote_dir}/spectre.pid"
    # Run spectre inside csh for Cadence env, then record PID from sh wrapper
    # csh runs spectre; sh wrapper handles PID tracking (csh $! syntax differs)
    csh_inner = f"{csh_body}; {spectre_command}"
    exec_cmd = (
        f"{env_setup}"
        f"mkdir -p {shlex.quote(remote_raw_dir)} && "
        f"csh -c {shlex.quote(csh_inner)} & "
        f"SPID=$!; echo $SPID > {shlex.quote(pid_file)}; wait $SPID"
    )

    logger.info("[remote] %s", exec_cmd)
    task = run_remote_task(
        runner,
        work_dir_base=remote_work_dir,
        run_id=run_id,
        uploads=uploads,
        command=exec_cmd,
        timeout=timeout,
    )

    if not task.success:
        return _SpectreRunResult(
            success=False, output_dir=None, returncode=-1,
            stdout=task.stdout, stderr=task.stderr,
            error=task.error or "Remote task failed",
            metadata={
                "timings": dict(task.timings),
                "command": exec_cmd,
                "spectre_command": spectre_command,
            },
        )

    if task.returncode != 0:
        stdout = (task.stdout or "").strip()
        stderr = (task.stderr or "").strip()
        if stdout or stderr:
            print("[Simulation output]")
            if stdout:
                print(stdout)
            if stderr:
                print("[stderr]")
                print(stderr)

    def _cleanup_remote_run_dir() -> None:
        if not keep_remote_files and task.remote_dir:
            try:
                runner.run_command(f"rm -rf {task.remote_dir}")
                logger.debug("Cleaned up remote Spectre directory: %s", task.remote_dir)
            except Exception:  # noqa: S110
                logger.warning("Failed to clean up remote Spectre directory: %s", task.remote_dir)

    try:
        base_output_dir = Path(base_output_dir).resolve()
        timings = dict(task.timings)
        download_total = 0.0

        started = time.perf_counter()
        result_download = runner.download(remote_raw_dir, base_output_dir / f"{netlist.stem}.raw", recursive=True)
        download_total += time.perf_counter() - started
        if result_download.returncode != 0:
            return _SpectreRunResult(
                success=False,
                output_dir=None,
                returncode=result_download.returncode,
                stdout=result_download.stdout,
                stderr=result_download.stderr,
                error=f"Failed to download remote raw results from {remote_raw_dir}: {result_download.stderr.strip()}",
                metadata={
                    "remote_host": runner.host,
                    "timings": timings | {"download_total": download_total},
                    "command": exec_cmd,
                    "spectre_command": spectre_command,
                },
            )

        for remote_name, local_name in (
            ("spectre.out", "spectre.out"),
            ("spectre.fc", "spectre.fc"),
            ("spectre.ic", "spectre.ic"),
        ):
            started = time.perf_counter()
            download_result = runner.download(f"{task.remote_dir}/{remote_name}", base_output_dir / local_name)
            download_total += time.perf_counter() - started
            if download_result.returncode != 0:
                logger.debug("Skipping optional remote file %s: %s", remote_name, download_result.stderr.strip())
        timings["download_total"] = download_total

        output_dir = base_output_dir / f"{netlist.stem}.raw"
        # Handle nested .raw/.raw from some spectre versions
        nested_raw = output_dir / f"{netlist.stem}.raw"
        if nested_raw.exists() and nested_raw.is_dir():
            output_dir = nested_raw

        return _SpectreRunResult(
            success=True,
            output_dir=output_dir,
            returncode=task.returncode,
            stdout=task.stdout,
            stderr=task.stderr,
            error=None,
            metadata={
                "remote_host": runner.host,
                "timings": timings,
                "command": exec_cmd,
                "spectre_command": spectre_command,
            },
        )
    finally:
        _cleanup_remote_run_dir()

# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------

def _build_simulation_result(
    run_result: _SpectreRunResult,
    output_format: str | None,
    extra_metadata: dict[str, Any] | None = None,
) -> SimulationResult:
    """Scan stdout/stderr for errors/warnings, parse PSF data, build result."""
    errors: list[str] = []
    warnings: list[str] = []
    combined = (run_result.stdout or "") + "\n" + (run_result.stderr or "")
    combined_lower = combined.lower()

    # Classify errors into short, actionable messages
    if "circuit read-in" in combined_lower:
        errors.append("netlist read error (missing include or syntax)")
    elif "license" in combined_lower and ("error" in combined_lower or "denied" in combined_lower):
        errors.append("license error")
    elif "convergence" in combined_lower:
        errors.append("convergence failure")
    elif "no such file" in combined_lower or "cannot open" in combined_lower:
        errors.append("file not found")
    elif "segmentation" in combined_lower or "core dump" in combined_lower:
        errors.append("spectre crashed")
    else:
        # Fallback: collect raw error lines
        for line in combined.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()
            if "error" in lower and "0 errors" not in lower:
                errors.append(stripped)

    for line in combined.splitlines():
        stripped = line.strip()
        if stripped:
            lower = stripped.lower()
            if "warning" in lower and "0 warnings" not in lower:
                warnings.append(stripped)

    output_dir = run_result.output_dir
    output_files = (
        [str(f) for f in output_dir.rglob("*") if f.is_file()]
        if output_dir and output_dir.exists()
        else []
    )

    if run_result.returncode != 0:
        status = ExecutionStatus.PARTIAL if output_files else ExecutionStatus.FAILURE
        if not errors:
            errors.append(f"exit code {run_result.returncode}")
    else:
        status = ExecutionStatus.SUCCESS

    data: dict[str, Any] = {}
    if output_dir and output_dir.exists() and output_format == "psfascii":
        data = parse_psf_ascii_directory(output_dir)

    metadata: dict[str, Any] = {
        "returncode": run_result.returncode,
        "output_dir": str(output_dir) if output_dir and output_dir.exists() else None,
        "output_files": output_files,
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    return SimulationResult(
        status=status,
        data=data,
        errors=errors,
        warnings=warnings,
        metadata=metadata,
    )

# ---------------------------------------------------------------------------
# SpectreSimulator
# ---------------------------------------------------------------------------

class SpectreSimulator:
    """Cadence Spectre simulator adapter."""

    def __init__(
        self,
        spectre_cmd: str = "spectre",
        spectre_args: list[str] | tuple[str, ...] | None = None,
        timeout: int = 600,
        work_dir: Path | None = None,
        output_format: str | None = "psfascii",
        remote_host: str | None = None,
        remote_user: str | None = None,
        remote_work_dir: str | None = None,
        jump_host: str | None = None,
        jump_user: str | None = None,
        ssh_key_path: Path | None = None,
        ssh_config_path: Path | None = None,
        keep_remote_files: bool = False,
        remote: bool = False,
        ssh_runner: SSHRunner | None = None,
        profile: str | None = None,
    ) -> None:
        load_vb_env()
        self._spectre_cmd = spectre_cmd
        self._spectre_args = list(spectre_args or [])
        self._timeout = timeout
        self._work_dir = work_dir
        self._output_format = output_format
        self._ssh_key_path = ssh_key_path
        self._ssh_config_path = ssh_config_path
        self._max_workers = 8
        self._pool: ThreadPoolExecutor | None = None
        self._keep_remote_files = keep_remote_files
        self._ssh_runner: SSHRunner | None = ssh_runner
        self._profile = profile

        rh, ru, jh, ju = remote_host, remote_user, jump_host, jump_user
        if remote:
            env = remote_ssh_env_from_os(profile)
            if rh is None:
                rh = env.remote_host
            if ru is None:
                ru = env.remote_user
            if jh is None:
                jh = env.jump_host
            if ju is None:
                ju = env.jump_user

        self._remote_host = rh
        self._remote_user = ru
        self._jump_host = jh
        self._jump_user = ju
        self._remote_work_dir_set = remote_work_dir is not None
        self._remote_work_dir = remote_work_dir

    # -- factory methods ----------------------------------------------------

    @classmethod
    def from_env(
        cls,
        spectre_cmd: str = "spectre",
        spectre_args: list[str] | tuple[str, ...] | None = None,
        timeout: int = 600,
        work_dir: Path | None = None,
        output_format: str | None = "psfascii",
        keep_remote_files: bool = False,
        ssh_runner: SSHRunner | None = None,
        profile: str | None = None,
    ) -> "SpectreSimulator":
        """Create a SpectreSimulator from environment variables.

        If the configured remote host is localhost (or unset with a localhost
        env var), returns a local simulator.  Otherwise automatically reuses
        the SSH connection managed by ``virtuoso-bridge start`` (via
        ControlMaster).  Raises RuntimeError if no remote connection is
        available.
        """
        load_vb_env()
        # Check if we should run locally
        suffix = f"_{profile}" if profile else ""
        remote_host = os.environ.get(f"VB_REMOTE_HOST{suffix}", "") or os.environ.get("VB_REMOTE_HOST", "")
        if remote_host and _is_localhost(remote_host):
            return cls(
                spectre_cmd=spectre_cmd,
                spectre_args=spectre_args,
                timeout=timeout,
                work_dir=work_dir,
                output_format=output_format,
                keep_remote_files=keep_remote_files,
                profile=profile,
            )

        if ssh_runner is None:
            from virtuoso_bridge.transport.tunnel import SSHClient
            if not SSHClient.is_running(profile):
                hint = f"Run `virtuoso-bridge start -p {profile}` first." if profile else "Run `virtuoso-bridge start` first."
                raise RuntimeError(f"No virtuoso-bridge connection found. {hint}")
            ssh_runner = SSHClient.from_env(keep_remote_files=keep_remote_files, profile=profile).ssh_runner

        return cls(
            spectre_cmd=spectre_cmd,
            spectre_args=spectre_args,
            timeout=timeout,
            work_dir=work_dir,
            output_format=output_format,
            keep_remote_files=keep_remote_files,
            remote=True,
            ssh_runner=ssh_runner,
            profile=profile,
        )

    @classmethod
    def local(
        cls,
        spectre_cmd: str = "spectre",
        spectre_args: list[str] | tuple[str, ...] | None = None,
        timeout: int = 600,
        work_dir: Path | None = None,
        output_format: str | None = "psfascii",
    ) -> "SpectreSimulator":
        """Create a SpectreSimulator for local execution (no SSH)."""
        return cls(
            spectre_cmd=spectre_cmd,
            spectre_args=spectre_args,
            timeout=timeout,
            work_dir=work_dir,
            output_format=output_format,
        )

    # -- public API ---------------------------------------------------------

    def run_simulation(self, netlist: Path, params: dict) -> SimulationResult:
        """Run a Spectre simulation on *netlist* synchronously."""
        netlist = Path(netlist).resolve()
        if not netlist.exists():
            return SimulationResult(
                status=ExecutionStatus.ERROR,
                errors=[f"Netlist file not found: {netlist}"],
            )
        if self._remote_host:
            return self._run_remote(netlist, params)
        return self._run_local(netlist)

    # -- parallel simulation API ---------------------------------------------

    def _ensure_pool(self) -> ThreadPoolExecutor:
        """Lazily create the thread pool on first submit."""
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self._max_workers)
        return self._pool

    def set_max_workers(self, n: int) -> None:
        """Change the maximum number of concurrent simulations.

        Takes effect on the next :meth:`submit` call if the pool hasn't been
        created yet, or after :meth:`shutdown` + next submit.
        """
        self._max_workers = n
        if self._pool is not None:
            logger.warning(
                "Pool already running with previous max_workers. "
                "Call shutdown() first to apply the new limit."
            )

    def submit(self, netlist: Path, params: dict | None = None) -> Future[SimulationResult]:
        """Submit a simulation to run in the background.

        Returns a :class:`~concurrent.futures.Future` immediately.  The
        simulation runs in a worker thread — each gets its own remote
        directory (uuid-based), so there are no file conflicts.  The SSH
        ControlMaster connection is shared automatically.

        Example::

            sim = SpectreSimulator.from_env()
            t1 = sim.submit(Path("tb_comparator.scs"))
            t2 = sim.submit(Path("tb_dac.scs"))
            # ... do other work ...
            result1 = t1.result()
            result2 = t2.result()
        """
        pool = self._ensure_pool()
        netlist = Path(netlist).resolve()
        params = params or {}
        return pool.submit(self.run_simulation, netlist, params)

    def run_parallel(
        self,
        tasks: list[tuple[Path, dict]],
        max_workers: int | None = None,
    ) -> list[SimulationResult]:
        """Submit multiple simulations and wait for all to complete.

        Convenience wrapper around :meth:`submit`.  For fire-and-forget or
        incremental submission, use :meth:`submit` directly.

        *max_workers* overrides the instance default for this batch only.
        """
        old = self._max_workers
        if max_workers is not None:
            self._max_workers = max_workers
            # Force new pool with the override
            self.shutdown()

        futures = [self.submit(Path(netlist), params) for netlist, params in tasks]
        results = self.wait_all(futures)

        if max_workers is not None:
            self._max_workers = old
            self.shutdown()

        return results

    @staticmethod
    def wait_all(futures: list[Future[SimulationResult]]) -> list[SimulationResult]:
        """Wait for all futures and return results in submission order.

        Failed simulations return an error result rather than raising.
        """
        results: list[SimulationResult] = []
        for i, future in enumerate(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(SimulationResult(
                    status=ExecutionStatus.ERROR,
                    errors=[f"Task {i} failed: {exc}"],
                ))
        passed = sum(1 for r in results if r.status == ExecutionStatus.SUCCESS)
        print(f"[parallel] Done: {passed}/{len(results)} succeeded")
        return results

    def shutdown(self) -> None:
        """Shut down the worker pool. A new pool is created on next submit."""
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def check_license(self) -> dict[str, Any]:
        """Check Spectre license availability on the remote host.

        Returns a dict with keys: ok, spectre_path, version, licenses.
        """
        if not self._remote_host or _is_localhost(self._remote_host):
            # Local mode: check spectre directly on this machine
            info: dict[str, Any] = {
                "ok": False,
                "spectre_path": None,
                "version": None,
                "licenses": [],
            }
            spectre_path = shutil.which(self._spectre_cmd)
            if spectre_path:
                info["spectre_path"] = spectre_path
                info["ok"] = True
                try:
                    ver = subprocess.run(
                        [self._spectre_cmd, "-V"],
                        capture_output=True, text=True, timeout=15,
                    )
                    ver_line = (ver.stdout or ver.stderr or "").strip().splitlines()
                    if ver_line:
                        info["version"] = ver_line[0]
                except Exception:
                    pass
                try:
                    lm = subprocess.run(
                        ["lmstat", "-a"],
                        capture_output=True, text=True, timeout=15,
                    )
                    for line in (lm.stdout or "").splitlines():
                        if "Users of" in line:
                            info["licenses"].append(line.strip())
                except Exception:
                    pass
            else:
                info["error"] = f"Spectre command '{self._spectre_cmd}' not found locally"
            return info

        runner = self._get_ssh_runner()

        # Build env setup: source cshrc in csh, then check spectre
        suffix = f"_{self._profile}" if self._profile else ""
        cadence_cshrc = shlex.quote(
            os.environ.get(f"VB_CADENCE_CSHRC{suffix}", "")
            or os.environ.get("VB_CADENCE_CSHRC", "")
        )
        check_script = (
            'HOSTNAME=`hostname 2>/dev/null || echo localhost`; export HOSTNAME && '
            f'export VB_CADENCE_CSHRC={cadence_cshrc} && '
            # Source Cadence env via csh, then run checks in sh
            f'eval "$(csh -c \'source {cadence_cshrc}; env\' 2>/dev/null '
            f'| grep -E \"^(PATH|LM_LICENSE_FILE|CDS)=\" '
            f'| sed \'s/^/export /\')" 2>/dev/null; '
            # 1. Which spectre
            'echo "SPECTRE_PATH=$(which spectre 2>/dev/null || echo NOTFOUND)"; '
            # 2. Spectre version
            'spectre -V 2>&1 | head -1; '
            # 3. lmstat for all features with active users
            'lmstat -a 2>/dev/null | grep -E "Users of" | grep "licenses in use" | grep -v "0 licenses in use"'
        )

        result = runner.run_command(check_script, timeout=30)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        info: dict[str, Any] = {
            "ok": False,
            "spectre_path": None,
            "version": None,
            "raw_output": stdout,
            "stderr": stderr,
            "licenses": [],
        }

        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("SPECTRE_PATH="):
                path = line.split("=", 1)[1]
                if path != "NOTFOUND":
                    info["spectre_path"] = path
            elif line.startswith("@(#)$CDS:"):
                info["version"] = line
            elif "Users of" in line:
                info["licenses"].append(line)

        if info["spectre_path"]:
            info["ok"] = True

        return info

    # -- private helpers ----------------------------------------------------

    def _run_local(self, netlist: Path) -> SimulationResult:
        run_result = _run_spectre_local(
            netlist=netlist,
            spectre_cmd=self._spectre_cmd,
            spectre_args=self._spectre_args,
            timeout=self._timeout,
            work_dir=self._work_dir,
            output_format=self._output_format,
        )
        if not run_result.success:
            return SimulationResult(
                status=ExecutionStatus.ERROR,
                errors=[run_result.error or "Spectre execution failed"],
            )
        return _build_simulation_result(run_result, self._output_format)

    def _get_ssh_runner(self) -> SSHRunner:
        if self._ssh_runner is None:
            self._ssh_runner = SSHRunner(
                host=self._remote_host,  # type: ignore[arg-type]
                user=self._remote_user,
                jump_host=self._jump_host,
                jump_user=self._jump_user,
                ssh_key_path=self._ssh_key_path,
                ssh_config_path=self._ssh_config_path,
                timeout=self._timeout,
                persistent_shell=True,
                verbose=True,
            )
        return self._ssh_runner

    def _run_remote(self, netlist: Path, params: dict) -> SimulationResult:
        runner = self._get_ssh_runner()
        timings: dict[str, float] = {}
        overall_started = time.perf_counter()

        if not self._remote_work_dir_set:
            username = resolve_remote_username(
                configured_user=self._remote_user or runner.user,
                runner=runner,
            )
            self._remote_work_dir = default_remote_spectre_work_dir(username)
            logger.info("Remote work dir: %s", self._remote_work_dir)
        if self._remote_work_dir is None:
            return SimulationResult(
                status=ExecutionStatus.ERROR,
                errors=["Remote work dir is not configured"],
            )

        base_output_dir = self._work_dir or netlist.parent
        run_result = _run_spectre_remote(
            netlist=netlist,
            params=params,
            runner=runner,
            remote_work_dir=self._remote_work_dir,
            base_output_dir=base_output_dir,
            spectre_cmd=self._spectre_cmd,
            spectre_args=self._spectre_args,
            output_format=self._output_format,
            timeout=self._timeout,
            keep_remote_files=self._keep_remote_files,
        )
        if not run_result.success:
            return SimulationResult(
                status=ExecutionStatus.ERROR,
                errors=[run_result.error or "Remote Spectre execution failed"],
                metadata=run_result.metadata,
            )
        timings.update(run_result.metadata.get("timings", {}))
        parse_started = time.perf_counter()
        result = _build_simulation_result(
            run_result,
            self._output_format,
            extra_metadata={
                "remote_host": self._remote_host,
                "timings": timings,
                "command": run_result.metadata.get("command"),
                "spectre_command": run_result.metadata.get("spectre_command"),
            },
        )
        timings["parse_results"] = time.perf_counter() - parse_started
        timings["total"] = time.perf_counter() - overall_started
        result.metadata["timings"] = timings
        return result

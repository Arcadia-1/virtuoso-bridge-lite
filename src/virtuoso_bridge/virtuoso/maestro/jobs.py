"""Durable handles for asynchronous Maestro simulations.

Starting a Maestro simulation is a CIW operation. Observing one is not: a
Cadence completion callback writes a small marker on the Virtuoso GUI host,
and later Python processes inspect that marker without sending more SKILL.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import socket
import time
import uuid
from dataclasses import dataclass
from getpass import getuser
from pathlib import Path
from typing import Any, Literal

from virtuoso_bridge.env import load_vb_env
from virtuoso_bridge.profile import resolve_profile
from virtuoso_bridge.runtime_paths import artifact_dir, tmp_dir
from virtuoso_bridge.transport.remote_paths import (
    default_virtuoso_bridge_dir,
    resolve_client_id,
    resolve_remote_username,
)
from virtuoso_bridge.transport.remote_roles import remote_host_roles_from_os
from virtuoso_bridge.transport.ssh import (
    SSHRunner,
    ssh_backend_env_from_os,
    ssh_proxy_url_from_os,
)
from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.requests import RequestHandle


MaestroJobTransport = Literal["ssh", "local"]

_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_PID = re.compile(r"^[1-9][0-9]*$")
_MANIFEST_SCHEMA = 2


@dataclass(frozen=True)
class MaestroJobEndpoint:
    """Filesystem and account identity that owns a durable job namespace."""

    transport: MaestroJobTransport
    configured_gui_host: str
    observed_gui_host: str
    account: str
    namespace: str


@dataclass(frozen=True)
class MaestroJob:
    """Stable identity and persisted locations for one Maestro run."""

    run_id: str
    work_dir: str
    local_dir: Path
    transport: MaestroJobTransport
    profile: str | None
    session: str
    history: str | None
    virtuoso_pid: int | None
    process_start: str | None
    target: dict[str, Any]
    endpoint: MaestroJobEndpoint
    request_handle: RequestHandle | None
    request_evidence: dict[str, Any]


@dataclass(frozen=True)
class MaestroJobStatus:
    """Read-only status snapshot safe to request repeatedly."""

    run_id: str
    state: str
    history: str | None
    session: str
    work_dir: str
    transport: MaestroJobTransport
    virtuoso_pid: int | None
    process_start: str | None
    completion: str | None
    diagnostics: tuple[str, ...]


class MaestroJobSubmissionError(RuntimeError):
    """Submission stopped with a durable run id and classified outcome."""

    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        state: str,
        request_handle: RequestHandle | None = None,
        request_evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.state = state
        self.request_handle = request_handle
        self.request_evidence = dict(request_evidence or {})


def _strip_skill_atom(raw: str) -> str:
    return (raw or "").strip().strip('"')


def _request_definitely_not_sent(exc: BaseException) -> bool:
    result = getattr(exc, "result", None)
    metadata = getattr(result, "metadata", {})
    return isinstance(metadata, dict) and metadata.get("request_sent") is False


def _request_evidence(source: object) -> tuple[RequestHandle | None, dict[str, Any]]:
    """Return a bounded, credential-free recovery record from a result/error."""
    result = getattr(source, "result", source)
    metadata = getattr(result, "metadata", {})
    if not isinstance(metadata, dict):
        return None, {}
    allowed = (
        "request_state",
        "outcome",
        "request_sent",
        "phase",
        "maestro_phase",
        "simulation_start_sent",
        "session",
        "completion_marker",
        "waiting_for_user",
    )
    evidence = {key: metadata[key] for key in allowed if key in metadata}
    handle = None
    if "request_handle" in metadata:
        try:
            handle = RequestHandle.model_validate(metadata["request_handle"])
        except Exception:
            evidence["invalid_request_handle"] = True
        else:
            evidence["request_handle"] = handle.model_dump()
    status = getattr(result, "status", None)
    if status is not None:
        evidence["status"] = getattr(status, "value", str(status))
    errors = [str(item) for item in (getattr(result, "errors", None) or [])]
    warnings = [str(item) for item in (getattr(result, "warnings", None) or [])]
    if errors:
        evidence["errors"] = errors[:8]
    if warnings:
        evidence["warnings"] = warnings[:8]
    output = getattr(result, "output", "") or ""
    if isinstance(output, str) and output:
        evidence["output"] = output[:4096]
    return handle, evidence


class MaestroJobManager:
    """Submit once through CIW, then observe completion out of band.

    Remote managers use the GUI host rather than the daemon or Spectre host:
    the callback is executed by the Virtuoso process on that host. Local
    managers use the local filesystem directly.
    """

    def __init__(
        self,
        runner: SSHRunner | None,
        *,
        work_root: str | Path,
        transport: MaestroJobTransport,
        local_root: Path | None = None,
        profile: str | None = None,
        client: object | None = None,
        owns_runner: bool = False,
        gui_host: str | None = None,
        observed_gui_host: str | None = None,
        account: str | None = None,
        local_gui_authorized: bool = False,
    ) -> None:
        if transport == "ssh" and runner is None:
            raise ValueError("SSH Maestro jobs require a runner")
        if transport == "local" and runner is not None:
            raise ValueError("local Maestro jobs must not use an SSH runner")
        self._runner = runner
        self._transport = transport
        self._work_root = self._normalize_work_root(work_root, transport)
        self._local_root = Path(local_root or artifact_dir("maestro-jobs")).resolve()
        self._profile = profile
        self._client = client
        self._owns_runner = owns_runner
        self._local_gui_authorized = local_gui_authorized
        if transport == "ssh":
            gui_host = gui_host or getattr(runner, "host", None)
            observed_gui_host = observed_gui_host or gui_host
            account = account or getattr(runner, "user", None)
            if not gui_host or not observed_gui_host or not account:
                raise ValueError(
                    "SSH Maestro jobs require a resolved GUI host and account identity"
                )
        else:
            gui_host = gui_host or socket.gethostname()
            observed_gui_host = observed_gui_host or gui_host
            account = account or getuser()
        self._endpoint = MaestroJobEndpoint(
            transport=transport,
            configured_gui_host=self._identity_text(gui_host, "configured GUI host"),
            observed_gui_host=self._identity_text(
                observed_gui_host, "observed GUI host"
            ),
            account=self._identity_text(account, "account"),
            namespace=self._work_root,
        )

    @property
    def local_root(self) -> Path:
        """Directory containing durable local job manifests."""
        return self._local_root

    @staticmethod
    def _identity_text(value: object, label: str) -> str:
        text = str(value or "").strip()
        if not text or any(character in text for character in "\x00\r\n"):
            raise ValueError(f"{label} identity must be nonempty and contain no controls")
        return text

    @staticmethod
    def _probe_ssh_identity(runner: SSHRunner) -> tuple[str, str]:
        result = runner.run_command(
            "host=$(hostname -f 2>/dev/null || hostname 2>/dev/null) && "
            "account=$(id -un 2>/dev/null || whoami 2>/dev/null) && "
            "printf 'host=%s\\naccount=%s\\n' \"$host\" \"$account\"",
            timeout=10,
        )
        if result.returncode:
            MaestroJobManager._raise_remote_error(
                result, "resolve Maestro GUI host identity"
            )
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        return (
            MaestroJobManager._identity_text(values.get("host"), "observed GUI host"),
            MaestroJobManager._identity_text(values.get("account"), "remote account"),
        )

    def _endpoint_dict(self) -> dict[str, str]:
        return {
            "transport": self._endpoint.transport,
            "configured_gui_host": self._endpoint.configured_gui_host,
            "observed_gui_host": self._endpoint.observed_gui_host,
            "account": self._endpoint.account,
            "namespace": self._endpoint.namespace,
        }

    @classmethod
    def from_env(
        cls,
        *,
        local_root: Path | None = None,
        remote_root: str | None = None,
        profile: str | None = None,
        timeout: int = 30,
    ) -> "MaestroJobManager":
        """Create an SSH-only observer from a Bridge profile."""
        profile = resolve_profile(profile)
        load_vb_env()
        roles = remote_host_roles_from_os(profile, load=False)
        if not roles.gui_host:
            raise RuntimeError("VB_GUI_HOST or VB_REMOTE_HOST is required for Maestro jobs")
        backend = ssh_backend_env_from_os(profile)
        runner = SSHRunner(
            host=roles.gui_host,
            user=roles.remote_user,
            jump_host=roles.jump_for(roles.gui_host),
            jump_user=roles.jump_user,
            timeout=timeout,
            persistent_shell=True,
            backend=backend.backend,
            max_sessions=backend.max_sessions,
            proxy_url=ssh_proxy_url_from_os(profile),
            verbose=True,
        )
        username = resolve_remote_username(
            configured_user=roles.remote_user or runner.user,
            runner=runner,
        )
        observed_host, observed_account = cls._probe_ssh_identity(runner)
        root = remote_root or default_virtuoso_bridge_dir(
            username, "maestro-jobs", resolve_client_id(profile)
        )
        return cls(
            runner,
            work_root=root,
            transport="ssh",
            local_root=local_root,
            profile=profile,
            owns_runner=True,
            gui_host=roles.gui_host,
            observed_gui_host=observed_host,
            account=observed_account,
        )

    @classmethod
    def local(
        cls,
        *,
        local_root: Path | None = None,
        work_root: Path | None = None,
    ) -> "MaestroJobManager":
        """Create a local-filesystem observer for locally running Virtuoso."""
        return cls(
            None,
            work_root=work_root or tmp_dir("maestro-jobs"),
            transport="local",
            local_root=local_root,
        )

    @classmethod
    def from_client(
        cls,
        client: object,
        *,
        local_root: Path | None = None,
        remote_root: str | None = None,
        local_work_root: Path | None = None,
        profile: str | None = None,
        local_gui: bool = False,
    ) -> "MaestroJobManager":
        """Create a submission manager bound to an already selected CIW."""
        if not isinstance(local_gui, bool):
            raise ValueError("local_gui must be an explicit boolean")
        runner = getattr(client, "gui_runner", None)
        if runner is None:
            if not local_gui:
                raise ValueError(
                    "No GUI SSH transport is attached. Pass local_gui=True only for "
                    "a genuinely local Virtuoso GUI, not a forwarded TCP endpoint."
                )
            return cls(
                None,
                work_root=local_work_root or tmp_dir("maestro-jobs"),
                transport="local",
                local_root=local_root,
                client=client,
                local_gui_authorized=True,
            )
        if local_gui:
            raise ValueError("local_gui=True conflicts with the attached GUI SSH transport")

        tunnel = getattr(client, "_tunnel", None)
        connected_profile = getattr(tunnel, "_profile", None)
        if profile is not None:
            requested_profile = resolve_profile(profile)
            if requested_profile != connected_profile:
                raise ValueError(
                    f"requested profile {requested_profile!r} does not match the "
                    f"connected client profile {connected_profile!r}"
                )
        profile = connected_profile
        load_vb_env()
        roles = remote_host_roles_from_os(profile, load=False)
        username = resolve_remote_username(
            configured_user=roles.remote_user or getattr(runner, "user", None),
            runner=runner,
        )
        observed_host, observed_account = cls._probe_ssh_identity(runner)
        root = remote_root or default_virtuoso_bridge_dir(
            username, "maestro-jobs", resolve_client_id(profile)
        )
        return cls(
            runner,
            work_root=root,
            transport="ssh",
            local_root=local_root,
            profile=profile,
            client=client,
            gui_host=getattr(runner, "host", None) or roles.gui_host,
            observed_gui_host=observed_host,
            account=observed_account,
        )

    def submit(
        self,
        *,
        session: str,
        run_id: str | None = None,
        run_mode: str = "",
        timeout: float = 180,
    ) -> MaestroJob:
        """Start one run and persist enough state for later polling.

        A submission whose acknowledgement is lost becomes ``unknown``. It is
        never retried because the first request may already have started a run.
        """
        if self._client is None:
            raise RuntimeError("submit requires MaestroJobManager.from_client()")
        if not session or not session.strip():
            raise ValueError("session must name one exact Maestro session")
        if isinstance(timeout, bool) or timeout <= 0 or not math.isfinite(timeout):
            raise ValueError("timeout must be positive and finite")
        session = session.strip()
        run_id = (run_id or f"mae-{uuid.uuid4().hex[:12]}").lower()
        self._validate_run_id(run_id)
        local_dir = self._local_root / run_id
        work_dir = self._job_work_dir(run_id)
        local_dir.mkdir(parents=True, exist_ok=False)

        manifest: dict[str, Any] = {
            "schema": _MANIFEST_SCHEMA,
            "kind": "maestro-simulation",
            "run_id": run_id,
            "transport": self._transport,
            "profile": self._profile,
            "endpoint": self._endpoint_dict(),
            "session": session,
            "run_mode": run_mode,
            "created_at": time.time(),
            "phase": "preparing",
            "history": None,
            "virtuoso_pid": None,
            "process_start": None,
            "target": {},
            "work_dir": work_dir,
            "request_handle": None,
            "request_evidence": {},
            "diagnostics": [],
        }
        self._write_manifest(local_dir, manifest)
        try:
            self._reserve_work_dir(work_dir, local_dir=local_dir)
        except Exception as exc:
            message = f"reserve Maestro job directory failed: {exc}"
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, "failed", message, persist_work=False
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(message, run_id=run_id, state="failed") from exc
        self._persist_work_manifest(work_dir, manifest, required=False)

        client = self._client
        preflight_phase = "dialog_guard"
        preflight_result = None
        try:
            dialogs = getattr(client, "dialogs")
            guard = dialogs.enable_guard(
                local_gui=self._transport == "local",
                protect_inflight=True,
                timeout=min(timeout, 30),
            )
            if getattr(guard, "status", None) != "clear":
                raise RuntimeError(
                    "connected CIW has a blocking or indeterminate dialog; "
                    "simulation was not started"
                )
            target = getattr(guard, "target", None)
            pid = getattr(target, "pid", None)
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise RuntimeError("dialog guard did not return a verified Virtuoso PID")
            preflight_phase = "session_state"
            state = client.maestro.get_session_state(session=session, timeout=min(timeout, 30))
            if getattr(state, "context", None) != "gui":
                raise RuntimeError(
                    f"session {session!r} is not an observed Maestro GUI session "
                    f"(context={getattr(state, 'context', 'unknown')})"
                )
            if getattr(state, "access", None) != "editing":
                raise RuntimeError(
                    f"session {session!r} is not editable "
                    f"(access={getattr(state, 'access', 'unknown')})"
                )
            manifest["virtuoso_pid"] = pid
            process_start = self._process_start_identity(pid)
            if not process_start:
                raise RuntimeError(
                    "could not bind the verified Virtuoso PID to a process instance"
                )
            manifest["process_start"] = process_start
            manifest["target"] = {
                "library": getattr(state, "lib", None),
                "cell": getattr(state, "cell", None),
                "view": getattr(state, "view", None),
                "application": getattr(state, "application", None),
                "unsaved": getattr(state, "unsaved", None),
            }
            self._write_manifest(local_dir, manifest)
            self._persist_text(work_dir, "virtuoso.pid", f"{pid}\n", required=True)
            self._persist_text(
                work_dir,
                "virtuoso.process_start",
                f"{process_start}\n",
                required=True,
            )
            self._persist_work_manifest(work_dir, manifest, required=True)
            callback_name = f"_vb_maestro_job_{uuid.uuid4().hex[:12]}"
            preflight_phase = "callback_setup"
            callback_result = client.execute_skill(
                self._callback_skill(callback_name, work_dir), timeout=min(timeout, 30)
            )
            preflight_result = callback_result
            errors = list(getattr(callback_result, "errors", []) or [])
            if errors:
                raise RuntimeError(f"callback setup failed: {errors[0]}")
        except Exception as exc:
            handle, evidence = _request_evidence(
                preflight_result if preflight_result is not None else exc
            )
            evidence.update(maestro_phase=preflight_phase, simulation_start_sent=False)
            manifest["request_handle"] = handle.model_dump() if handle else None
            manifest["request_evidence"] = evidence
            outcome = (
                "unknown" if handle is not None
                and evidence.get("request_sent") is not False
                and evidence.get("request_state") != "completed" else "failed"
            )
            message = f"Maestro preflight failed before simulation start: {exc}"
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, outcome, message, persist_work=True
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(
                message, run_id=run_id, state=outcome,
                request_handle=handle, request_evidence=evidence,
            ) from exc

        try:
            # Exactly one non-idempotent start request. Never wrap this in a retry.
            raw_history = client.maestro.run_simulation(
                session=session,
                callback=callback_name,
                run_mode=run_mode,
                timeout=timeout,
            )
        except Exception as exc:
            outcome = "failed" if _request_definitely_not_sent(exc) else "unknown"
            request_handle, evidence = _request_evidence(exc)
            evidence["maestro_phase"] = "simulation_start"
            manifest["request_handle"] = (
                request_handle.model_dump() if request_handle is not None else None
            )
            manifest["request_evidence"] = evidence
            message = (
                "Maestro start was not acknowledged; the request was not repeated. "
                f"Verify this job by status before any new submission: {exc}"
            )
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, outcome, message, persist_work=True
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(
                message,
                run_id=run_id,
                state=outcome,
                request_handle=request_handle,
                request_evidence=evidence,
            ) from exc

        history = _strip_skill_atom(raw_history)
        if not history or history == "nil":
            message = (
                "maeRunSimulation returned no history acknowledgement; the request was not repeated"
            )
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, "unknown", message, persist_work=True
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(message, run_id=run_id, state="unknown")

        try:
            manifest["phase"] = "submitted"
            manifest["history"] = history
            manifest["submitted_at"] = time.time()
            self._write_manifest(local_dir, manifest)
            try:
                self._append_event(work_dir, f"submitted {history}")
            except Exception:
                manifest["diagnostics"].append(
                    "could not append submitted lifecycle event"
                )
                self._write_manifest(local_dir, manifest)
            self._persist_work_manifest(work_dir, manifest, required=False)
            return self.load(run_id)
        except Exception as exc:
            message = (
                "Maestro start was acknowledged, but durable state persistence failed; "
                "the request was not repeated. Use the attached run_id to inspect before "
                f"any new submission: {exc}"
            )
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, "unknown", message, persist_work=True
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(
                message, run_id=run_id, state="unknown"
            ) from exc

    def load(self, run_id: str, *, migrate_legacy: bool = False) -> MaestroJob:
        """Reattach to a job, requiring explicit migration of schema-1 handles."""
        run_id = run_id.lower()
        self._validate_run_id(run_id)
        local_dir = self._local_root / run_id
        try:
            manifest = json.loads((local_dir / "manifest.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"No local manifest for Maestro job {run_id}") from exc
        if manifest.get("run_id") != run_id or manifest.get("kind") != "maestro-simulation":
            raise RuntimeError(f"Invalid Maestro manifest for {run_id}")
        schema = manifest.get("schema")
        if schema == 1 and migrate_legacy:
            manifest = self._migrate_legacy_manifest(local_dir, manifest)
        elif schema != _MANIFEST_SCHEMA:
            suffix = (
                "; pass migrate_legacy=True to bind this schema-1 handle to the "
                "current endpoint explicitly"
                if schema == 1
                else ""
            )
            raise RuntimeError(
                f"Unsupported Maestro manifest schema for {run_id}: {schema!r}{suffix}"
            )
        transport = manifest.get("transport")
        if transport not in ("ssh", "local"):
            raise RuntimeError(f"Invalid Maestro transport for {run_id}: {transport!r}")
        endpoint_raw = manifest.get("endpoint")
        if not isinstance(endpoint_raw, dict):
            raise RuntimeError(f"Maestro manifest {run_id} has no endpoint identity")
        try:
            endpoint = MaestroJobEndpoint(
                transport=endpoint_raw["transport"],
                configured_gui_host=self._identity_text(
                    endpoint_raw["configured_gui_host"], "configured GUI host"
                ),
                observed_gui_host=self._identity_text(
                    endpoint_raw["observed_gui_host"], "observed GUI host"
                ),
                account=self._identity_text(endpoint_raw["account"], "account"),
                namespace=str(endpoint_raw["namespace"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Maestro manifest {run_id} has an invalid endpoint identity"
            ) from exc
        if endpoint.transport != transport:
            raise RuntimeError(
                f"Maestro manifest {run_id} endpoint transport is inconsistent"
            )
        request_handle = None
        if manifest.get("request_handle") is not None:
            try:
                request_handle = RequestHandle.model_validate(manifest["request_handle"])
            except Exception as exc:
                raise RuntimeError(
                    f"Maestro manifest {run_id} has an invalid request handle"
                ) from exc
        return MaestroJob(
            run_id=run_id,
            work_dir=str(manifest["work_dir"]),
            local_dir=local_dir,
            transport=transport,
            profile=manifest.get("profile"),
            session=str(manifest["session"]),
            history=manifest.get("history"),
            virtuoso_pid=manifest.get("virtuoso_pid"),
            process_start=manifest.get("process_start"),
            target=dict(manifest.get("target") or {}),
            endpoint=endpoint,
            request_handle=request_handle,
            request_evidence=dict(manifest.get("request_evidence") or {}),
        )

    def list(self) -> list[MaestroJob]:
        """List local handles without contacting Virtuoso or the job host."""
        if not self._local_root.exists():
            return []
        jobs: list[tuple[float, MaestroJob]] = []
        for path in self._local_root.glob("*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                job = self.load(str(manifest["run_id"]))
                jobs.append((float(manifest.get("created_at", 0)), job))
            except (
                AttributeError,
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
                OSError,
                json.JSONDecodeError,
            ):
                continue
        return [job for _, job in sorted(jobs, key=lambda item: item[0], reverse=True)]

    def reconcile(
        self,
        job: MaestroJob,
        *,
        client: object | None = None,
        timeout: float = 10,
        local_gui: bool = False,
    ) -> MaestroJob:
        """Query the original request receipt without resubmitting the run."""
        self._validate_job(job)
        if isinstance(timeout, bool) or timeout <= 0 or not math.isfinite(timeout):
            raise ValueError("timeout must be positive and finite")
        client = client or self._client
        if client is None:
            raise ValueError("reconcile requires the original endpoint's VirtuosoClient")
        self._validate_recovery_client(
            client,
            job,
            local_gui=local_gui or self._local_gui_authorized,
        )
        if job.request_handle is None:
            raise ValueError("job has no recoverable request handle")

        result = client.requests.receipt(job.request_handle, timeout=timeout)
        returned_handle, evidence = _request_evidence(result)
        if returned_handle != job.request_handle:
            raise RuntimeError("receipt did not preserve the original request handle")
        manifest = json.loads(
            (job.local_dir / "manifest.json").read_text(encoding="utf-8")
        )
        manifest["request_handle"] = job.request_handle.model_dump()
        request_phase = job.request_evidence.get("maestro_phase", "simulation_start")
        evidence["maestro_phase"] = request_phase
        manifest["request_evidence"] = evidence
        manifest["reconciled_at"] = time.time()
        history = _strip_skill_atom(getattr(result, "output", "") or "")
        if request_phase in ("session_state", "callback_setup"):
            evidence["simulation_start_sent"] = False
            settled = (
                evidence.get("request_state") == "completed"
                or evidence.get("request_sent") is False
            )
            manifest["phase"] = "failed" if settled else "unknown"
            event = f"reconciled preflight {request_phase}; simulation not started"
            manifest.setdefault("diagnostics", []).append(event)
        elif getattr(result, "ok", False) and history and history != "nil":
            manifest["phase"] = "submitted"
            manifest["history"] = history
            event = f"reconciled {history}"
        elif evidence.get("request_sent") is False:
            manifest["phase"] = "failed"
            event = "reconciled not-started"
        else:
            manifest["phase"] = "unknown"
            event = "reconciled unknown"
        self._write_manifest(job.local_dir, manifest)
        try:
            self._append_event(job.work_dir, event)
        except Exception as exc:
            manifest.setdefault("diagnostics", []).append(
                f"could not append reconciliation lifecycle event: {exc}"
            )
            self._write_manifest(job.local_dir, manifest)
        self._persist_work_manifest(job.work_dir, manifest, required=False)
        return self.load(job.run_id)

    def status(self, job: MaestroJob) -> MaestroJobStatus:
        """Inspect one job without sending SKILL to the CIW."""
        self._validate_job(job)
        manifest = json.loads((job.local_dir / "manifest.json").read_text(encoding="utf-8"))
        phase = str(manifest.get("phase", "unknown"))
        diagnostic_items = [str(item) for item in manifest.get("diagnostics", [])]
        if phase == "failed":
            return self._status(job, "failed", None, tuple(diagnostic_items))

        observed = self._observe_work_dir(job.work_dir)
        completion = observed.get("marker") or None
        pid_text = observed.get("pid", "")
        observed_pid = int(pid_text) if _PID.fullmatch(pid_text) else job.virtuoso_pid
        identity_matches = (
            bool(job.process_start)
            and bool(_PID.fullmatch(pid_text))
            and observed_pid == job.virtuoso_pid
            and observed.get("process_start") == job.process_start
        )
        observed_history = _strip_skill_atom(observed.get("callback_history", ""))
        observed_session = _strip_skill_atom(observed.get("callback_session", ""))
        callback_matches = (
            observed_session == job.session
            and bool(observed_history)
            and (job.history is None or observed_history == job.history)
        )
        if observed.get("exists") != "1":
            state = "missing"
        elif observed.get("completed") == "1" and callback_matches and identity_matches:
            state = "completed"
        elif observed.get("completed") == "1":
            state = "unknown"
            diagnostic_items.append(
                "completion callback or process identity did not match the submitted job"
            )
        elif (phase == "submitted" and identity_matches
              and observed.get("process_match") == "1"):
            state = "running"
        else:
            state = "unknown"
        return MaestroJobStatus(
            run_id=job.run_id,
            state=state,
            history=job.history or observed_history or None,
            session=job.session,
            work_dir=job.work_dir,
            transport=job.transport,
            virtuoso_pid=observed_pid,
            process_start=job.process_start,
            completion=completion,
            diagnostics=tuple(diagnostic_items),
        )

    def log(self, job: MaestroJob, *, lines: int = 80) -> str:
        """Tail lifecycle events, not simulator waveform or Spectre output."""
        self._validate_job(job)
        if lines < 1 or lines > 10_000:
            raise ValueError("lines must be in 1..10000")
        if self._transport == "local":
            path = Path(job.work_dir) / "events.log"
            content = path.read_text(encoding="utf-8")
            return "".join(content.splitlines(keepends=True)[-lines:])
        assert self._runner is not None
        result = self._runner.run_command(
            f"tail -n {lines} {shlex.quote(job.work_dir + '/events.log')} 2>/dev/null"
        )
        if result.returncode:
            self._raise_remote_error(result, "read Maestro lifecycle log")
        return result.stdout

    def close(self) -> None:
        if self._owns_runner and self._runner is not None:
            self._runner.close()

    def __enter__(self) -> "MaestroJobManager":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _normalize_work_root(
        work_root: str | Path, transport: MaestroJobTransport
    ) -> str:
        if transport == "local":
            return str(Path(work_root).expanduser().resolve())
        root = str(work_root).rstrip("/")
        if not root.startswith("/"):
            raise ValueError("remote Maestro job root must be an absolute POSIX path")
        return root

    def _job_work_dir(self, run_id: str) -> str:
        if self._transport == "local":
            return str(Path(self._work_root) / "jobs" / run_id)
        return f"{self._work_root}/jobs/{run_id}"

    def _reserve_work_dir(self, work_dir: str, *, local_dir: Path) -> None:
        if self._transport == "local":
            path = Path(work_dir)
            if path.resolve() == local_dir.resolve():
                event_path = path / "events.log"
                if event_path.exists():
                    raise FileExistsError(work_dir)
            else:
                path.mkdir(parents=True, exist_ok=False)
            event_path = path / "events.log"
            event_path.write_text("", encoding="utf-8")
            return
        assert self._runner is not None
        parent = work_dir.rsplit("/", 1)[0]
        result = self._runner.run_command(
            f"mkdir -p {shlex.quote(parent)} && "
            f"mkdir {shlex.quote(work_dir)} 2>/dev/null || "
            f"{{ echo 'remote run id already exists or is unavailable' >&2; exit 2; }}; "
            f": > {shlex.quote(work_dir + '/events.log')}"
        )
        if result.returncode:
            self._raise_remote_error(result, "reserve remote Maestro job directory")

    def _persist_text(
        self, work_dir: str, name: str, content: str, *, required: bool
    ) -> None:
        if self._transport == "local":
            path = Path(work_dir) / name
            temporary = path.with_name(path.name + ".tmp")
            try:
                temporary.write_text(content, encoding="utf-8")
                self._atomic_replace(temporary, path)
            except OSError:
                if required:
                    raise
            return
        assert self._runner is not None
        result = self._runner.upload_text(content, f"{work_dir}/{name}")
        if required and result.returncode:
            self._raise_remote_error(result, f"persist remote Maestro {name}")

    def _persist_work_manifest(
        self, work_dir: str, manifest: dict[str, Any], *, required: bool
    ) -> None:
        self._persist_text(
            work_dir,
            "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True),
            required=required,
        )

    def _append_event(self, work_dir: str, event: str) -> None:
        if self._transport == "local":
            with (Path(work_dir) / "events.log").open("a", encoding="utf-8") as stream:
                stream.write(event + "\n")
            return
        assert self._runner is not None
        result = self._runner.run_command(
            f"printf '%s\\n' {shlex.quote(event)} >> {shlex.quote(work_dir + '/events.log')}"
        )
        if result.returncode:
            self._raise_remote_error(result, "append Maestro lifecycle event")

    def _observe_work_dir(self, work_dir: str) -> dict[str, str]:
        if self._transport == "local":
            path = Path(work_dir)
            marker_path = path / "completed"
            pid_path = path / "virtuoso.pid"
            start_path = path / "virtuoso.process_start"
            marker = ""
            callback_session = ""
            callback_history = ""
            if marker_path.is_file() and marker_path.stat().st_size:
                marker_lines = marker_path.read_text(encoding="utf-8").splitlines()
                marker = marker_lines[0]
                for line in marker_lines[1:]:
                    if line.startswith("session="):
                        callback_session = line.removeprefix("session=")
                    elif line.startswith("history="):
                        callback_history = line.removeprefix("history=")
            pid = pid_path.read_text(encoding="utf-8").strip() if pid_path.is_file() else ""
            process_start = (
                start_path.read_text(encoding="utf-8").strip()
                if start_path.is_file()
                else ""
            )
            current_start = (
                self._process_start_identity(int(pid)) if _PID.fullmatch(pid) else None
            )
            process_match = bool(process_start and current_start == process_start)
            return {
                "exists": "1" if path.is_dir() else "0",
                "completed": "1" if marker else "0",
                "alive": "1" if current_start else "0",
                "process_match": "1" if process_match else "0",
                "process_start": process_start,
                "marker": marker,
                "callback_session": callback_session,
                "callback_history": callback_history,
                "pid": pid,
            }

        assert self._runner is not None
        command = (
            f"d={shlex.quote(work_dir)}; exists=0; completed=0; alive=0; "
            "process_match=0; marker=; callback_session=; callback_history=; "
            "pid=; expected_start=; current_start=; "
            "if test -d \"$d\"; then exists=1; fi; "
            "if test -s \"$d/completed\"; then completed=1; "
            "marker=$(sed -n '1p' \"$d/completed\"); "
            "callback_session=$(sed -n '2s/^session=//p' \"$d/completed\"); "
            "callback_history=$(sed -n '3s/^history=//p' \"$d/completed\"); fi; "
            "if test -r \"$d/virtuoso.pid\"; then pid=$(cat \"$d/virtuoso.pid\"); fi; "
            "if test -r \"$d/virtuoso.process_start\"; then "
            "expected_start=$(cat \"$d/virtuoso.process_start\"); fi; "
            "case \"$pid\" in ''|*[!0-9]*) alive=0;; "
            "*) if test \"$pid\" -gt 0 2>/dev/null && kill -0 \"$pid\" 2>/dev/null; then "
            "alive=1; stat=$(cat \"/proc/$pid/stat\" 2>/dev/null || true); "
            "rest=${stat#*) }; set -- $rest; "
            "if test $# -ge 20; then current_start=linux:${20}; fi; "
            "if test -n \"$expected_start\" && "
            "test \"$current_start\" = \"$expected_start\"; then process_match=1; fi; "
            "fi;; esac; "
            "printf 'exists=%s\\ncompleted=%s\\nalive=%s\\nprocess_match=%s\\n"
            "process_start=%s\\nmarker=%s\\ncallback_session=%s\\n"
            "callback_history=%s\\npid=%s\\n' "
            "\"$exists\" \"$completed\" \"$alive\" \"$process_match\" \"$expected_start\" "
            "\"$marker\" \"$callback_session\" \"$callback_history\" \"$pid\""
        )
        result = self._runner.run_command(command)
        if result.returncode:
            self._raise_remote_error(result, "query Maestro job status")
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def _process_start_identity(self, pid: int) -> str | None:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        if self._transport == "ssh":
            assert self._runner is not None
            result = self._runner.run_command(
                f"cat /proc/{pid}/stat 2>/dev/null", timeout=10
            )
            if result.returncode:
                return None
            return self._linux_process_start(result.stdout)
        if os.name == "nt":
            return self._windows_process_start(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return None
        return self._linux_process_start(stat)

    @staticmethod
    def _linux_process_start(stat: str) -> str | None:
        _prefix, separator, tail = stat.strip().rpartition(") ")
        if not separator:
            return None
        fields = tail.split()
        if len(fields) < 20 or not fields[19].isdigit():
            return None
        return f"linux:{fields[19]}"

    @staticmethod
    def _windows_process_start(pid: int) -> str | None:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"windows:{ticks}"
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if os.name == "nt":
            return MaestroJobManager._windows_pid_alive(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    def _windows_pid_alive(pid: int) -> bool:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must use lowercase letters, digits, and hyphens")

    def _migrate_legacy_manifest(
        self, local_dir: Path, manifest: dict[str, Any]
    ) -> dict[str, Any]:
        run_id = str(manifest.get("run_id") or "")
        transport = manifest.get("transport")
        if transport != self._transport:
            raise ValueError("legacy job transport does not match this manager")
        if transport == "ssh" and manifest.get("profile") != self._profile:
            raise ValueError("legacy job profile does not match this manager")
        if str(manifest.get("work_dir")) != self._job_work_dir(run_id):
            raise ValueError("legacy job namespace does not match this manager")
        migrated = dict(manifest)
        migrated.update(
            schema=_MANIFEST_SCHEMA,
            endpoint=self._endpoint_dict(),
            process_start=None,
            request_handle=None,
            request_evidence={},
            migrated_at=time.time(),
        )
        migrated.setdefault("diagnostics", []).append(
            "schema-1 handle explicitly rebound to the selected endpoint; "
            "PID liveness is not trusted without original process-start evidence"
        )
        self._write_manifest(local_dir, migrated)
        return migrated

    def _validate_recovery_client(
        self, client: object, job: MaestroJob, *, local_gui: bool
    ) -> None:
        runner = getattr(client, "gui_runner", None)
        if job.transport == "ssh":
            if local_gui or runner is None:
                raise ValueError(
                    "SSH job recovery requires the original GUI SSH transport"
                )
            observed_host, account = self._probe_ssh_identity(runner)
            candidate = MaestroJobEndpoint(
                transport="ssh",
                configured_gui_host=self._identity_text(
                    getattr(runner, "host", None), "configured GUI host"
                ),
                observed_gui_host=observed_host,
                account=account,
                namespace=self._work_root,
            )
        else:
            if runner is not None or not local_gui:
                raise ValueError(
                    "local job recovery requires explicit local_gui=True and no GUI SSH transport"
                )
            candidate = self._endpoint
        if candidate != job.endpoint:
            raise ValueError("recovery client endpoint identity does not match the job")

    def _validate_job(self, job: MaestroJob) -> None:
        self._validate_run_id(job.run_id)
        if job.local_dir.name != job.run_id:
            raise ValueError("job local directory does not match run_id")
        if job.transport != self._transport:
            raise ValueError(
                f"job transport {job.transport!r} does not match manager {self._transport!r}"
            )
        if job.transport == "ssh" and job.profile != self._profile:
            raise ValueError(
                f"job profile {job.profile!r} does not match manager {self._profile!r}"
            )
        if job.endpoint != self._endpoint:
            raise ValueError(
                "job GUI host/account/namespace identity does not match this manager"
            )
        if job.work_dir != self._job_work_dir(job.run_id):
            raise ValueError("job work directory does not match the manager namespace")
        if job.transport == "ssh":
            assert self._runner is not None
            host, account = self._probe_ssh_identity(self._runner)
            if (host != job.endpoint.observed_gui_host
                    or account != job.endpoint.account):
                raise ValueError("current SSH host/account identity no longer matches the job")

    @staticmethod
    def _write_manifest(local_dir: Path, manifest: dict[str, Any]) -> None:
        path = local_dir / "manifest.json"
        temporary = local_dir / "manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        MaestroJobManager._atomic_replace(temporary, path)

    @staticmethod
    def _atomic_replace(source: Path, destination: Path) -> None:
        """Replace a small state file, tolerating brief Windows file scans."""
        for attempt in range(3):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.02 * (attempt + 1))

    def _record_outcome(
        self,
        local_dir: Path,
        work_dir: str,
        manifest: dict[str, Any],
        phase: str,
        diagnostic: str,
        *,
        persist_work: bool,
    ) -> list[str]:
        manifest["phase"] = phase
        manifest.setdefault("diagnostics", []).append(diagnostic)
        manifest["updated_at"] = time.time()
        errors: list[str] = []
        try:
            self._write_manifest(local_dir, manifest)
        except Exception as exc:
            errors.append(f"local manifest: {exc}")
        if persist_work:
            try:
                self._persist_work_manifest(work_dir, manifest, required=False)
            except Exception as exc:
                errors.append(f"work manifest: {exc}")
        return errors

    @staticmethod
    def _with_persistence_errors(message: str, errors: list[str]) -> str:
        if not errors:
            return message
        return message + "; state persistence also failed: " + "; ".join(errors)

    @staticmethod
    def _callback_skill(name: str, work_dir: str) -> str:
        marker = work_dir.rstrip("/") + "/completed"
        temporary = marker + ".tmp"
        events = work_dir.rstrip("/") + "/events.log"
        shell = (
            f"mv {shlex.quote(temporary)} {shlex.quote(marker)} && "
            f"printf 'completed %s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >> "
            f"{shlex.quote(events)}"
        )
        command = "sh -c " + shlex.quote(shell)
        return (
            f"procedure({name}(session runID)\n"
            "  let((vbJobPort)\n"
            f'    vbJobPort=outfile("{escape_skill_string(temporary)}")\n'
            "    when(vbJobPort\n"
            '      fprintf(vbJobPort "completed\\nsession=%L\\nhistory=%L\\n" session runID)\n'
            "      close(vbJobPort)\n"
            f'      system("{escape_skill_string(command)}")\n'
            "    )\n"
            "  )\n"
            "  t\n"
            ")"
        )

    @staticmethod
    def _status(
        job: MaestroJob,
        state: str,
        completion: str | None,
        diagnostics: tuple[str, ...],
    ) -> MaestroJobStatus:
        return MaestroJobStatus(
            run_id=job.run_id,
            state=state,
            history=job.history,
            session=job.session,
            work_dir=job.work_dir,
            transport=job.transport,
            virtuoso_pid=job.virtuoso_pid,
            process_start=job.process_start,
            completion=completion,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _raise_remote_error(result: object, action: str) -> None:
        code = getattr(result, "returncode", -1)
        stderr = str(getattr(result, "stderr", "")).strip()
        raise RuntimeError(f"{action} failed (rc={code}): {stderr}")

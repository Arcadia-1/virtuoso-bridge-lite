"""Result helpers and local orchestration for XStream GDS export."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from numbers import Real
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, cast

from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.transport.remote_paths import (
    default_virtuoso_bridge_dir,
    resolve_client_id,
    sanitize_username_for_path,
)
from virtuoso_bridge.virtuoso.layout.xstream import (
    XStreamExportRequest,
    XStreamLogResult,
    _parse_xstream_request_response,
    parse_xstream_log,
    xstream_export_gds_skill,
)
from virtuoso_bridge.virtuoso.response import response_fields


CleanupPolicy = Literal["success", "always", "never"]

_MONOTONIC = time.monotonic
_SLEEP = time.sleep
_CLEANUP_POLICIES = ("success", "always", "never")
_MAX_FINAL_LOG_REFRESHES = 3
_SOCKET_TIMEOUT_RE = re.compile(
    r"Socket timeout after \d+(?:\.\d+)?(?:[eE][+-]?\d+)?s"
)
_REMOTE_SENTINEL_TOKEN_RE = re.compile(r"VBXSTREAM_[A-Za-z0-9]+")
_REMOTE_SHA256_RE = re.compile(r"[0-9A-Fa-f]{64}")
_REMOTE_LOG_TAIL_BYTES = 128 * 1024
_REMOTE_POLL_OUTPUT_LIMIT = _REMOTE_LOG_TAIL_BYTES + 4096
_REMOTE_HASH_CHUNK_BYTES = 1024 * 1024
_REMOTE_STAGE_CREATED = "VBXSTREAM_STAGE_CREATED"
_REMOTE_STAGE_READY = "VBXSTREAM_STAGE_READY"


class GdsExportReason(str, Enum):
    """Final reason for a GDS export result."""

    COMPLETED = "completed"
    XSTREAM_FAILURE = "xstream_failure"
    XSTREAM_ERRORS = "xstream_errors"
    REQUEST_CLEANUP_ERROR = "request_cleanup_error"
    SKILL_ERROR = "skill_error"
    LAUNCH_INDETERMINATE = "launch_indeterminate"
    INCOMPLETE_LOG = "incomplete_log"
    MISSING_GDS = "missing_gds"
    EMPTY_GDS = "empty_gds"
    MALFORMED_LOG = "malformed_log"
    STAGING_ERROR = "staging_error"
    TRANSPORT_ERROR = "transport_error"
    PUBLICATION_ERROR = "publication_error"


@dataclass(frozen=True)
class GdsExportResult:
    """Immutable outcome of one GDS export attempt."""

    status: ExecutionStatus
    reason: GdsExportReason
    timed_out: bool
    library: str
    cell: str
    view: str
    execution_time: float
    local_gds_path: Path | None = None
    local_log_path: Path | None = None
    log_result: XStreamLogResult | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    remote_run_dir: str | None = None
    local_run_dir: Path | None = None
    remote_files_retained: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def ok(self) -> bool:
        """Return whether the export completed successfully."""
        return self.status == ExecutionStatus.SUCCESS


@dataclass(frozen=True)
class _ExportInputs:
    library: str
    cell: str
    view: str
    output_path: Path
    log_path: Path
    stream_map: Path
    timeout: float
    poll_interval: float
    skill_timeout: float
    finalization_reserve: float
    cleanup_policy: CleanupPolicy


@dataclass(frozen=True)
class _ExportPaths:
    run_dir: Path
    gds: Path
    log: Path
    snapshot_log: Path
    diagnostic_log: Path


@dataclass(frozen=True)
class _RemoteExportPaths:
    owned_root: PurePosixPath
    run_dir: PurePosixPath
    gds: PurePosixPath
    log: PurePosixPath
    stream_map: PurePosixPath


@dataclass(frozen=True)
class _RemoteLogFinalization:
    observation: _ArtifactObservation
    log: XStreamLogResult | None
    local_path: Path | None
    stable: bool
    error: str | None = None
    reason: GdsExportReason | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class _ArtifactObservation:
    log_present: bool = False
    log_size: int = 0
    log_bytes: bytes = b""
    log_text: str = ""
    log_tail_truncated: bool = False
    log_digest: str | None = None
    gds_present: bool = False
    gds_size: int = 0
    gds_digest: str | None = None


@dataclass(frozen=True)
class _PollOutcome:
    observation: _ArtifactObservation
    log: XStreamLogResult | None
    saw_evidence: bool
    deadline_expired: bool
    staging_error: str | None = None


class _LogSnapshotChanged(RuntimeError):
    def __init__(self, outcome: _PollOutcome) -> None:
        super().__init__("XStream log changed before GDS commit")
        self.outcome = outcome


class _RemoteSnapshotChanged(RuntimeError):
    def __init__(self, observation: _ArtifactObservation) -> None:
        super().__init__("remote XStream artifacts changed before publication")
        self.observation = observation


class _RemoteFinalizationFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        reason: GdsExportReason,
        *,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.timed_out = timed_out


def _remote_security_prelude() -> str:
    return r"""umask 077
vb_uid=$(id -u) || exit $?
vb_read_meta() {
    vb_meta=$(stat -c '%u %a' -- "$1" 2>/dev/null) ||
        vb_meta=$(stat -f '%u %Lp' -- "$1" 2>/dev/null) || return 74
    set -- $vb_meta
    vb_owner=$1
    vb_mode=$2
}
vb_verify_dir() {
    vb_path=$1
    if [ -L "$vb_path" ] || [ ! -d "$vb_path" ]; then
        printf 'unsafe remote XStream directory: %s\n' "$vb_path" >&2
        return 73
    fi
    vb_read_meta "$vb_path" || return $?
    if [ "$vb_owner" != "$vb_uid" ]; then
        printf 'remote XStream directory has wrong owner: %s\n' "$vb_path" >&2
        return 75
    fi
    if [ "$vb_mode" != 700 ]; then
        chmod 700 -- "$vb_path" || return $?
        if [ -L "$vb_path" ] || [ ! -d "$vb_path" ]; then
            printf 'unsafe remote XStream directory after chmod: %s\n' "$vb_path" >&2
            return 73
        fi
        vb_read_meta "$vb_path" || return $?
        if [ "$vb_owner" != "$vb_uid" ] || [ "$vb_mode" != 700 ]; then
            printf 'remote XStream directory is not private: %s\n' "$vb_path" >&2
            return 76
        fi
    fi
}
vb_ensure_dir() {
    vb_path=$1
    if [ -L "$vb_path" ]; then
        printf 'remote XStream directory is a symlink: %s\n' "$vb_path" >&2
        return 73
    fi
    if [ ! -e "$vb_path" ]; then
        mkdir -m 700 -- "$vb_path" || return $?
    fi
    vb_verify_dir "$vb_path"
}
"""


def _remote_private_chain(
    paths: _RemoteExportPaths,
) -> tuple[PurePosixPath, PurePosixPath, PurePosixPath]:
    return paths.owned_root.parent.parent, paths.owned_root.parent, paths.owned_root


def _remote_stage_command(paths: _RemoteExportPaths) -> str:
    commands = [_remote_security_prelude()]
    for directory in _remote_private_chain(paths):
        commands.append(
            f"vb_ensure_dir {shlex.quote(directory.as_posix())} || exit $?\n"
        )
    quoted_run = shlex.quote(paths.run_dir.as_posix())
    commands.extend(
        [
            f"if [ -L {quoted_run} ] || [ -e {quoted_run} ]; then\n",
            f"    printf 'remote XStream run already exists: %s\\n' {quoted_run} >&2\n",
            "    exit 77\n",
            "fi\n",
            f"mkdir -m 700 -- {quoted_run} || exit $?\n",
            f"printf '%s\\n' {_REMOTE_STAGE_CREATED}\n",
            f"vb_verify_dir {quoted_run} || exit $?\n",
            f"printf '%s\\n' {_REMOTE_STAGE_READY}\n",
        ]
    )
    return "".join(commands)


def _remote_stage_markers(output: str) -> tuple[bool, bool]:
    lines = output.splitlines()
    created_count = lines.count(_REMOTE_STAGE_CREATED)
    ready_count = lines.count(_REMOTE_STAGE_READY)
    if created_count > 1 or ready_count > 1 or ready_count > created_count:
        raise ValueError("malformed remote XStream staging markers")
    return created_count == 1, ready_count == 1


def _remote_delete_command(
    paths: _RemoteExportPaths,
    *,
    remove_run: bool,
) -> str:
    commands = [_remote_security_prelude()]
    for directory in (*_remote_private_chain(paths), paths.run_dir):
        commands.append(
            f"vb_verify_dir {shlex.quote(directory.as_posix())} || exit $?\n"
        )
    target = paths.run_dir if remove_run else paths.gds
    flags = "-rf" if remove_run else "-f"
    commands.append(f"rm {flags} -- {shlex.quote(target.as_posix())}\n")
    return "".join(commands)


def _remote_poll_command(
    log_path: PurePosixPath,
    gds_path: PurePosixPath,
    token: str,
    *,
    include_digests: bool = False,
) -> str:
    if _REMOTE_SENTINEL_TOKEN_RE.fullmatch(token) is None:
        raise ValueError("invalid remote XStream sentinel token")
    quoted_log = shlex.quote(log_path.as_posix())
    quoted_gds = shlex.quote(gds_path.as_posix())
    tail_probe_bytes = _REMOTE_LOG_TAIL_BYTES + 1
    quoted_tail_probe = shlex.quote(
        (log_path.parent / f".{token}.tail-probe").as_posix()
    )
    quoted_tail_lines = shlex.quote(
        (log_path.parent / f".{token}.tail-lines").as_posix()
    )
    digest_function = ""
    log_digest = ""
    gds_digest = ""
    if include_digests:
        digest_function = r"""vb_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        vb_digest_line=$(sha256sum -- "$1") || return $?
        vb_digest=${vb_digest_line%% *}
    elif command -v shasum >/dev/null 2>&1; then
        vb_digest_line=$(shasum -a 256 -- "$1") || return $?
        vb_digest=${vb_digest_line%% *}
    elif command -v openssl >/dev/null 2>&1; then
        vb_digest_line=$(openssl dgst -sha256 "$1") || return $?
        vb_digest=${vb_digest_line##* }
    else
        printf 'no SHA-256 tool available\n' >&2
        return 78
    fi
    printf '%s\n' "$vb_digest"
}
"""
        log_digest = (
            f"vb_log_digest=$(vb_sha256 {quoted_log}) || exit $?; "
            f"printf '%s LOG_SHA256 %s\\n' {token} \"$vb_log_digest\"; "
        )
        gds_digest = (
            f"vb_gds_digest=$(vb_sha256 {quoted_gds}) || exit $?; "
            f"printf '%s GDS_SHA256 %s\\n' {token} \"$vb_gds_digest\"; "
        )
    return (
        digest_function
        + f"if [ -f {quoted_log} ]; then "
        f"vb_log_size=$(wc -c < {quoted_log}) || exit $?; "
        f"printf '%s LOG_SIZE %s\\n' {token} \"$vb_log_size\"; "
        f"{log_digest}"
        "umask 077; "
        f"vb_tail_probe={quoted_tail_probe}; "
        f"vb_tail_lines={quoted_tail_lines}; "
        'rm -f -- "$vb_tail_probe" "$vb_tail_lines" || exit $?; '
        "trap 'rm -f -- \"$vb_tail_probe\" \"$vb_tail_lines\"' 0; "
        f"tail -c {tail_probe_bytes} -- {quoted_log} "
        '> "$vb_tail_probe"; vb_tail_rc=$?; '
        '[ "$vb_tail_rc" -eq 0 ] || exit "$vb_tail_rc"; '
        'tail -n 200 -- "$vb_tail_probe" > "$vb_tail_lines"; '
        "vb_tail_rc=$?; "
        '[ "$vb_tail_rc" -eq 0 ] || exit "$vb_tail_rc"; '
        'vb_tail_size=$(wc -c < "$vb_tail_lines"); vb_tail_rc=$?; '
        '[ "$vb_tail_rc" -eq 0 ] || exit "$vb_tail_rc"; '
        f"if [ \"$vb_tail_size\" -gt {_REMOTE_LOG_TAIL_BYTES} ]; then "
        "vb_tail_truncated=1; else vb_tail_truncated=0; fi; "
        f"printf '%s LOG_TRUNCATED %s\\n' {token} "
        '"$vb_tail_truncated"; '
        f"printf '%s LOG_BEGIN\\n' {token}; "
        f'tail -c {_REMOTE_LOG_TAIL_BYTES} -- "$vb_tail_lines"; '
        "vb_tail_rc=$?; printf '\\n'; "
        f"[ \"$vb_tail_rc\" -eq 0 ] || exit \"$vb_tail_rc\"; "
        'rm -f -- "$vb_tail_probe" "$vb_tail_lines"; '
        "vb_tail_rc=$?; trap - 0; "
        '[ "$vb_tail_rc" -eq 0 ] || exit "$vb_tail_rc"; '
        f"printf '%s LOG_END\\n' {token}; "
        f"else printf '%s LOG_MISSING\\n' {token}; fi; "
        f"if [ -f {quoted_gds} ]; then "
        f"vb_gds_size=$(wc -c < {quoted_gds}) || exit $?; "
        f"printf '%s GDS_SIZE %s\\n' {token} \"$vb_gds_size\"; "
        f"{gds_digest}"
        f"else printf '%s GDS_MISSING\\n' {token}; fi"
    )


def _parse_remote_poll_output(
    output: str,
    token: str,
    *,
    require_digests: bool = False,
) -> _ArtifactObservation:
    if not isinstance(output, str):
        raise TypeError("remote XStream sentinel output must be text")
    if _REMOTE_SENTINEL_TOKEN_RE.fullmatch(token) is None:
        raise ValueError("invalid remote XStream sentinel token")
    if len(output) > _REMOTE_POLL_OUTPUT_LIMIT:
        raise ValueError(
            "remote XStream sentinel output exceeds remote XStream "
            "sentinel limit"
        )

    prefix = f"{token} "
    log_missing = False
    log_size: int | None = None
    log_digest: str | None = None
    log_started = False
    log_finished = False
    log_tail_truncated: bool | None = None
    gds_missing = False
    gds_size: int | None = None
    gds_digest: str | None = None
    frame_lines: list[str] = []

    for line in output.splitlines():
        if log_started and not log_finished:
            if line == f"{token} LOG_END":
                log_finished = True
            else:
                frame_lines.append(line)
            continue
        if not line.startswith(prefix):
            raise ValueError(
                f"malformed remote XStream sentinel output: {line!r}"
            )
        fields = line[len(prefix) :].split()
        if fields == ["LOG_MISSING"]:
            if log_missing or log_size is not None or log_started:
                raise ValueError("malformed remote XStream sentinel log status")
            log_missing = True
        elif len(fields) == 2 and fields[0] == "LOG_SIZE":
            if log_missing or log_size is not None or log_started:
                raise ValueError("malformed remote XStream sentinel log size")
            if not fields[1].isdigit():
                raise ValueError("malformed remote XStream sentinel log size")
            log_size = int(fields[1])
        elif len(fields) == 2 and fields[0] == "LOG_SHA256":
            if (
                log_missing
                or log_size is None
                or log_started
                or log_digest is not None
                or _REMOTE_SHA256_RE.fullmatch(fields[1]) is None
            ):
                raise ValueError(
                    "malformed remote XStream sentinel log digest"
                )
            log_digest = fields[1].lower()
        elif fields == ["LOG_BEGIN"]:
            if log_missing or log_size is None or log_started:
                raise ValueError("malformed remote XStream sentinel log frame")
            log_started = True
        elif len(fields) == 2 and fields[0] == "LOG_TRUNCATED":
            if (
                log_missing
                or log_size is None
                or log_started
                or log_tail_truncated is not None
                or fields[1] not in {"0", "1"}
            ):
                raise ValueError(
                    "malformed remote XStream sentinel truncation marker"
                )
            log_tail_truncated = fields[1] == "1"
        elif fields == ["GDS_MISSING"]:
            if gds_missing or gds_size is not None:
                raise ValueError("malformed remote XStream sentinel GDS status")
            gds_missing = True
        elif len(fields) == 2 and fields[0] == "GDS_SIZE":
            if gds_missing or gds_size is not None:
                raise ValueError("malformed remote XStream sentinel GDS size")
            if not fields[1].isdigit():
                raise ValueError("malformed remote XStream sentinel GDS size")
            gds_size = int(fields[1])
        elif len(fields) == 2 and fields[0] == "GDS_SHA256":
            if (
                gds_missing
                or gds_size is None
                or gds_digest is not None
                or _REMOTE_SHA256_RE.fullmatch(fields[1]) is None
            ):
                raise ValueError(
                    "malformed remote XStream sentinel GDS digest"
                )
            gds_digest = fields[1].lower()
        else:
            raise ValueError(
                f"malformed remote XStream sentinel control line: {line!r}"
            )

    log_present = log_size is not None
    if log_missing == log_present:
        raise ValueError("malformed remote XStream sentinel log status")
    if log_present and not (log_started and log_finished):
        raise ValueError("malformed remote XStream sentinel log frame")
    if log_missing and (log_started or log_finished):
        raise ValueError("malformed remote XStream sentinel log frame")
    if log_missing and log_tail_truncated is not None:
        raise ValueError("malformed remote XStream sentinel truncation marker")
    if require_digests and log_present and log_digest is None:
        raise ValueError("missing remote XStream sentinel log digest")

    gds_present = gds_size is not None
    if gds_missing == gds_present:
        raise ValueError("malformed remote XStream sentinel GDS status")
    if require_digests and gds_present and gds_digest is None:
        raise ValueError("missing remote XStream sentinel GDS digest")

    log_text = "\n".join(frame_lines) if log_present else ""
    log_bytes = log_text.encode("utf-8")
    return _ArtifactObservation(
        log_present=log_present,
        log_size=log_size or 0,
        log_bytes=log_bytes,
        log_text=log_text,
        log_tail_truncated=bool(log_tail_truncated),
        log_digest=log_digest,
        gds_present=gds_present,
        gds_size=gds_size or 0,
        gds_digest=gds_digest,
    )


def _validate_export_inputs(
    library: str,
    cell: str,
    output_path: str | Path,
    *,
    stream_map: str | Path,
    view: str,
    log_path: str | Path | None,
    timeout: float,
    poll_interval: float,
    skill_timeout: float,
    finalization_reserve: float,
    cleanup_policy: CleanupPolicy,
) -> _ExportInputs:
    normalized_library = _require_nonempty_string("library", library)
    normalized_cell = _require_nonempty_string("cell", cell)
    normalized_view = _require_nonempty_string("view", view)

    normalized_output = Path(output_path).expanduser().resolve()
    normalized_log = (
        Path(log_path).expanduser().resolve()
        if log_path is not None
        else normalized_output.with_name(
            f"{normalized_output.stem}.xstream.log"
        )
    )
    normalized_stream_map = Path(stream_map).expanduser().resolve()
    if not normalized_stream_map.is_file():
        raise FileNotFoundError(
            f"stream_map is not an existing regular file: {normalized_stream_map}"
        )
    if (
        _paths_alias(normalized_output, normalized_log)
        or _paths_alias(normalized_output, normalized_stream_map)
        or _paths_alias(normalized_log, normalized_stream_map)
    ):
        raise ValueError("output_path, log_path, and stream_map must be distinct")

    normalized_timeout = _positive_finite_float("timeout", timeout)
    normalized_poll_interval = _positive_finite_float(
        "poll_interval", poll_interval
    )
    normalized_skill_timeout = _positive_finite_float(
        "skill_timeout", skill_timeout
    )
    normalized_finalization_reserve = _positive_finite_float(
        "finalization_reserve", finalization_reserve
    )
    if normalized_finalization_reserve >= normalized_timeout:
        raise ValueError("finalization_reserve must be smaller than timeout")
    if cleanup_policy not in _CLEANUP_POLICIES:
        raise ValueError(
            "cleanup_policy must be one of: success, always, never"
        )

    return _ExportInputs(
        library=normalized_library,
        cell=normalized_cell,
        view=normalized_view,
        output_path=normalized_output,
        log_path=normalized_log,
        stream_map=normalized_stream_map,
        timeout=normalized_timeout,
        poll_interval=normalized_poll_interval,
        skill_timeout=normalized_skill_timeout,
        finalization_reserve=normalized_finalization_reserve,
        cleanup_policy=cast(CleanupPolicy, cleanup_policy),
    )


def _require_nonempty_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _paths_alias(first: Path, second: Path) -> bool:
    if first == second:
        return True
    if not first.exists() or not second.exists():
        return False
    try:
        return first.samefile(second)
    except OSError:
        return False


def _positive_finite_float(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite positive real number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError(f"{name} must be a finite positive real number")
    return normalized


class _BudgetExpired(TimeoutError):
    """Raised when an export operation has exhausted its time budget."""


@dataclass(frozen=True)
class _Budget:
    started_at: float
    deadline: float
    prefinalization_deadline: float
    clock: Callable[[], float]

    @classmethod
    def start(
        cls,
        timeout: float,
        finalization_reserve: float,
        clock: Callable[[], float] = _MONOTONIC,
    ) -> _Budget:
        started_at = float(clock())
        deadline = started_at + float(timeout)
        return cls(
            started_at=started_at,
            deadline=deadline,
            prefinalization_deadline=deadline - float(finalization_reserve),
            clock=clock,
        )

    def remaining(self, finalizing: bool) -> float:
        deadline = self.deadline if finalizing else self.prefinalization_deadline
        return max(0.0, deadline - float(self.clock()))

    def timeout(self, finalizing: bool, cap: float | None = None) -> float:
        remaining = self.remaining(finalizing)
        timeout = remaining if cap is None else min(remaining, cap)
        if timeout <= 0.0:
            raise _BudgetExpired("export time budget exhausted")
        return float(timeout)

    def elapsed(self) -> float:
        return max(0.0, float(self.clock()) - self.started_at)


def _is_indeterminate_skill_timeout(errors: tuple[str, ...]) -> bool:
    """Return whether every error is one exact transport timeout form."""
    return bool(errors) and all(
        error == "SKILL execution timeout in Virtuoso"
        or _SOCKET_TIMEOUT_RE.fullmatch(error) is not None
        for error in errors
    )


def _classify_export(
    *,
    cleanup_failures: tuple[str, ...],
    log: XStreamLogResult | None,
    skill_errors: tuple[str, ...],
    launch_indeterminate: bool,
    saw_evidence: bool,
    gds_present: bool,
    gds_size: int,
    gds_published: bool,
    deadline_expired: bool,
) -> tuple[ExecutionStatus, GdsExportReason, bool]:
    if cleanup_failures:
        return (
            ExecutionStatus.ERROR,
            GdsExportReason.REQUEST_CLEANUP_ERROR,
            deadline_expired,
        )
    if log is not None and log.terminal_failures:
        return (
            ExecutionStatus.FAILURE,
            GdsExportReason.XSTREAM_FAILURE,
            deadline_expired,
        )

    completed = log is not None and log.completed
    if completed and (log.error_count is None or log.warning_count is None):
        return (
            ExecutionStatus.ERROR,
            GdsExportReason.MALFORMED_LOG,
            deadline_expired,
        )
    if completed and log.error_count != 0:
        return (
            ExecutionStatus.FAILURE,
            GdsExportReason.XSTREAM_ERRORS,
            deadline_expired,
        )
    if skill_errors and not _is_indeterminate_skill_timeout(skill_errors):
        return ExecutionStatus.ERROR, GdsExportReason.SKILL_ERROR, False
    valid_completion = (
        completed and log.error_count == 0 and log.warning_count is not None
    )
    if valid_completion and not gds_present:
        return ExecutionStatus.PARTIAL, GdsExportReason.MISSING_GDS, True
    if valid_completion and gds_size <= 0:
        return ExecutionStatus.PARTIAL, GdsExportReason.EMPTY_GDS, True
    if not valid_completion and (not launch_indeterminate or saw_evidence):
        return ExecutionStatus.PARTIAL, GdsExportReason.INCOMPLETE_LOG, True
    if not valid_completion and launch_indeterminate and not saw_evidence:
        return (
            ExecutionStatus.PARTIAL,
            GdsExportReason.LAUNCH_INDETERMINATE,
            True,
        )
    if valid_completion and gds_present and gds_size > 0 and gds_published:
        return ExecutionStatus.SUCCESS, GdsExportReason.COMPLETED, False
    raise ValueError("unclassifiable GDS export observations")


def export_gds(
    client: object,
    library: str,
    cell: str,
    output_path: str | Path,
    *,
    stream_map: str | Path,
    view: str = "layout",
    log_path: str | Path | None = None,
    timeout: float = 300.0,
    poll_interval: float = 0.5,
    skill_timeout: float = 30.0,
    finalization_reserve: float = 30.0,
    cleanup_policy: CleanupPolicy = "success",
    recovery_hook: Callable[[], object] | None = None,
) -> GdsExportResult:
    """Export one layout to GDS using a fresh XStream staging directory."""
    inputs = _validate_export_inputs(
        library,
        cell,
        output_path,
        stream_map=stream_map,
        view=view,
        log_path=log_path,
        timeout=timeout,
        poll_interval=poll_interval,
        skill_timeout=skill_timeout,
        finalization_reserve=finalization_reserve,
        cleanup_policy=cleanup_policy,
    )
    budget = _Budget.start(
        inputs.timeout,
        inputs.finalization_reserve,
        clock=_MONOTONIC,
    )
    try:
        ssh_runner = getattr(client, "ssh_runner")
    except Exception as exc:
        return GdsExportResult(
            status=ExecutionStatus.ERROR,
            reason=GdsExportReason.TRANSPORT_ERROR,
            timed_out=budget.remaining(finalizing=True) <= 0.0,
            library=inputs.library,
            cell=inputs.cell,
            view=inputs.view,
            execution_time=budget.elapsed(),
            errors=(f"failed to inspect client ssh_runner: {exc}",),
        )
    if ssh_runner is None:
        return _export_gds_local(client, inputs, budget, recovery_hook)
    return _export_gds_remote(client, inputs, budget, recovery_hook)


def _export_gds_remote(
    client: object,
    inputs: _ExportInputs,
    budget: _Budget,
    recovery_hook: Callable[[], object] | None,
) -> GdsExportResult:
    try:
        runner = getattr(client, "ssh_runner")
        configured_user = getattr(runner, "user", None)
    except Exception as exc:
        return _remote_error_result(
            inputs,
            budget,
            f"failed to inspect remote XStream runner: {exc}",
        )

    if isinstance(configured_user, str) and configured_user.strip():
        username = sanitize_username_for_path(configured_user)
    else:
        try:
            whoami = runner.run_command(
                "whoami",
                timeout=budget.timeout(finalizing=False),
            )
            returncode, stdout, stderr = _command_result_fields(whoami)
        except Exception as exc:
            return _remote_error_result(
                inputs,
                budget,
                f"failed to resolve remote XStream username: {exc}",
                timed_out=_exception_timed_out(exc),
            )
        if returncode != 0 or not stdout.strip():
            detail = stderr.strip() or stdout.strip() or (
                f"whoami returned exit status {returncode}"
            )
            return _remote_error_result(
                inputs,
                budget,
                f"failed to resolve remote XStream username: {detail}",
            )
        username = sanitize_username_for_path(stdout)

    try:
        client_id = resolve_client_id()
        owned_root = PurePosixPath(
            default_virtuoso_bridge_dir(
                username,
                "xstream",
                client_id,
            ).replace("\\", "/")
        )
        if not owned_root.is_absolute() or ".." in owned_root.parts:
            raise ValueError("remote XStream scratch root must be absolute")
        run_dir = owned_root / uuid.uuid4().hex
        paths = _RemoteExportPaths(
            owned_root=owned_root,
            run_dir=run_dir,
            gds=run_dir / "output.gds",
            log=run_dir / "xstream.log",
            stream_map=run_dir / "stream.map",
        )
    except Exception as exc:
        return _remote_error_result(
            inputs,
            budget,
            f"failed to allocate remote XStream staging path: {exc}",
        )

    remote_run_dir = paths.run_dir.as_posix()
    try:
        mkdir_result = runner.run_command(
            _remote_stage_command(paths),
            timeout=budget.timeout(finalizing=False),
        )
        returncode, stdout, stderr = _command_result_fields(mkdir_result)
        run_created, run_ready = _remote_stage_markers(stdout)
    except Exception as exc:
        return _remote_error_result(
            inputs,
            budget,
            f"failed to create remote XStream staging: {exc}",
            remote_run_dir=remote_run_dir,
            remote_files_retained=None,
            timed_out=_exception_timed_out(exc),
        )
    if returncode != 0 or not run_ready:
        detail = stderr.strip() or stdout.strip() or (
            f"secure staging returned exit status {returncode}"
        )
        return _remote_error_result(
            inputs,
            budget,
            f"failed to create remote XStream staging: {detail}",
            remote_run_dir=remote_run_dir,
            remote_files_retained=True if run_created else None,
        )

    warnings: list[str] = []
    try:
        upload_response = getattr(client, "upload_file")(
            inputs.stream_map,
            paths.stream_map.as_posix(),
            timeout=budget.timeout(finalizing=False),
        )
        warnings.extend(_response_warnings(upload_response))
        upload_errors, upload_status, _upload_output = response_fields(
            upload_response
        )
    except Exception as exc:
        return _remote_error_result(
            inputs,
            budget,
            f"failed to upload remote XStream stream map: {exc}",
            warnings=warnings,
            remote_run_dir=remote_run_dir,
            remote_files_retained=True,
            timed_out=_exception_timed_out(exc),
        )
    if not _response_succeeded(upload_status):
        detail = "; ".join(upload_errors) or (
            "upload returned non-success status without errors: "
            f"{upload_status!r}"
        )
        return _remote_error_result(
            inputs,
            budget,
            f"failed to upload remote XStream stream map: {detail}",
            warnings=warnings,
            remote_run_dir=remote_run_dir,
            remote_files_retained=True,
        )

    request = XStreamExportRequest(
        library=inputs.library,
        top_cell=inputs.cell,
        view=inputs.view,
        stream_file=paths.gds.as_posix(),
        layer_map=paths.stream_map.as_posix(),
        log_file=paths.log.as_posix(),
        run_dir=remote_run_dir,
    )
    skill_errors: tuple[str, ...] = ()
    cleanup_failures: tuple[str, ...] = ()
    launch_indeterminate = False
    should_poll = False
    try:
        skill_code = xstream_export_gds_skill(request)
        response = getattr(client, "execute_skill")(
            skill_code,
            timeout=budget.timeout(
                finalizing=False,
                cap=inputs.skill_timeout,
            ),
        )
        warnings.extend(_response_warnings(response))
    except _BudgetExpired as exc:
        return _remote_error_result(
            inputs,
            budget,
            f"remote XStream launch skipped: {exc}",
            warnings=warnings,
            remote_run_dir=remote_run_dir,
            remote_files_retained=True,
            timed_out=True,
        )
    except Exception as exc:
        if _exception_timed_out(exc):
            skill_errors = ("SKILL execution timeout in Virtuoso",)
            launch_indeterminate = True
            should_poll = True
        else:
            skill_errors = (f"SKILL execution failed: {exc}",)
    else:
        try:
            response_errors, response_status, response_output = response_fields(
                response
            )
        except Exception as exc:
            skill_errors = (f"failed to normalize SKILL response: {exc}",)
        else:
            skill_errors = tuple(response_errors)
            response_succeeded = _response_succeeded(response_status)
            if not response_succeeded and not skill_errors:
                skill_errors = (
                    "SKILL request returned non-success status without errors: "
                    f"{response_status!r}",
                )
            if not response_succeeded or skill_errors:
                launch_indeterminate = _is_indeterminate_skill_timeout(
                    skill_errors
                )
                should_poll = launch_indeterminate
            else:
                try:
                    request_response = _parse_xstream_request_response(
                        response_output
                    )
                except ValueError as exc:
                    skill_errors = (str(exc),)
                else:
                    cleanup_failures = request_response.cleanup_failures
                    if request_response.state == "failed":
                        skill_errors = (
                            request_response.body_error
                            or "XStream request body failed",
                        )
                    elif not cleanup_failures:
                        should_poll = True

    outcome = _poll_remote_artifacts(
        runner,
        paths,
        budget,
        inputs.poll_interval,
        should_poll=should_poll,
        launch_indeterminate=launch_indeterminate,
        recovery_hook=recovery_hook,
        warnings=warnings,
    )
    return _finalize_remote_export(
        client,
        runner,
        inputs,
        paths,
        budget,
        outcome,
        skill_errors=skill_errors,
        cleanup_failures=cleanup_failures,
        launch_indeterminate=launch_indeterminate,
        warnings=warnings,
    )


def _command_result_fields(result: object) -> tuple[int, str, str]:
    if isinstance(result, dict):
        returncode = result.get("returncode")
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
    else:
        returncode = getattr(result, "returncode")
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise TypeError("remote command returncode must be an integer")
    return returncode, str(stdout or ""), str(stderr or "")


def _exception_timed_out(exc: BaseException) -> bool:
    return isinstance(exc, (_BudgetExpired, subprocess.TimeoutExpired))


def _remote_error_result(
    inputs: _ExportInputs,
    budget: _Budget,
    error: str,
    *,
    warnings: list[str] | tuple[str, ...] = (),
    remote_run_dir: str | None = None,
    remote_files_retained: bool | None = None,
    timed_out: bool = False,
    reason: GdsExportReason = GdsExportReason.TRANSPORT_ERROR,
) -> GdsExportResult:
    return GdsExportResult(
        status=ExecutionStatus.ERROR,
        reason=reason,
        timed_out=(
            timed_out or budget.remaining(finalizing=True) <= 0.0
        ),
        library=inputs.library,
        cell=inputs.cell,
        view=inputs.view,
        execution_time=budget.elapsed(),
        errors=(error,),
        warnings=tuple(warnings),
        remote_run_dir=remote_run_dir,
        remote_files_retained=remote_files_retained,
    )


def _poll_remote_artifacts(
    runner: object,
    paths: _RemoteExportPaths,
    budget: _Budget,
    poll_interval: float,
    *,
    should_poll: bool,
    launch_indeterminate: bool,
    recovery_hook: Callable[[], object] | None,
    warnings: list[str],
) -> _PollOutcome:
    observation = _ArtifactObservation()
    log: XStreamLogResult | None = None
    saw_evidence = False
    recovery_attempted = False

    while True:
        if budget.remaining(finalizing=False) <= 0.0:
            return _PollOutcome(
                observation=observation,
                log=log,
                saw_evidence=saw_evidence,
                deadline_expired=True,
            )
        try:
            observation = _observe_remote_artifacts(
                runner,
                paths,
                budget,
                finalizing=False,
            )
            log = (
                parse_xstream_log(observation.log_text)
                if observation.log_present
                else None
            )
        except Exception as exc:
            return _PollOutcome(
                observation=observation,
                log=log,
                saw_evidence=saw_evidence,
                deadline_expired=(
                    _exception_timed_out(exc)
                    or budget.remaining(finalizing=False) <= 0.0
                ),
                staging_error=f"failed to poll remote XStream artifacts: {exc}",
            )

        progress = _observation_has_evidence(observation, log)
        saw_evidence = saw_evidence or progress
        if (
            launch_indeterminate
            and progress
            and not recovery_attempted
            and recovery_hook is not None
            and budget.remaining(finalizing=False) > 0.0
        ):
            recovery_attempted = True
            try:
                recovery_hook()
            except Exception as exc:
                warnings.append(f"recovery hook failed: {exc}")

        if _observation_is_terminal(observation, log) or not should_poll:
            return _PollOutcome(
                observation=observation,
                log=log,
                saw_evidence=saw_evidence,
                deadline_expired=False,
            )
        remaining = budget.remaining(finalizing=False)
        if remaining <= 0.0:
            return _PollOutcome(
                observation=observation,
                log=log,
                saw_evidence=saw_evidence,
                deadline_expired=True,
            )
        delay = min(poll_interval, remaining)
        try:
            _SLEEP(delay)
        except (OSError, OverflowError) as exc:
            return _PollOutcome(
                observation=observation,
                log=log,
                saw_evidence=saw_evidence,
                deadline_expired=budget.remaining(finalizing=False) <= 0.0,
                staging_error=f"failed while polling remote artifacts: {exc}",
            )


def _observe_remote_artifacts(
    runner: object,
    paths: _RemoteExportPaths,
    budget: _Budget,
    *,
    finalizing: bool,
) -> _ArtifactObservation:
    token = f"VBXSTREAM_{uuid.uuid4().hex}"
    command = _remote_poll_command(
        paths.log,
        paths.gds,
        token,
        include_digests=finalizing,
    )
    result = getattr(runner, "run_command")(
        command,
        timeout=budget.timeout(finalizing=finalizing),
    )
    returncode, stdout, stderr = _command_result_fields(result)
    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or (
            f"remote sentinel returned exit status {returncode}"
        )
        raise OSError(detail)
    return _parse_remote_poll_output(
        stdout,
        token,
        require_digests=finalizing,
    )


def _remote_log_fingerprint(
    observation: _ArtifactObservation,
) -> tuple[bool, int, bytes, str | None]:
    return (
        observation.log_present,
        observation.log_size,
        observation.log_bytes,
        observation.log_digest,
    )


def _remote_gds_fingerprint(
    observation: _ArtifactObservation,
) -> tuple[bool, int, str | None]:
    return (
        observation.gds_present,
        observation.gds_size,
        observation.gds_digest,
    )


def _remote_download_temp_path(destination: Path) -> Path:
    return destination.parent / f".vbd-{uuid.uuid4().hex}.tmp"


def _download_remote_file(
    client: object,
    remote_path: PurePosixPath,
    local_path: Path,
    budget: _Budget,
    warnings: list[str],
    *,
    label: str,
) -> None:
    try:
        budget.timeout(finalizing=True)
        local_path.parent.mkdir(parents=True, exist_ok=True)
    except _BudgetExpired as exc:
        raise _RemoteFinalizationFailure(
            str(exc),
            GdsExportReason.PUBLICATION_ERROR,
            timed_out=True,
        ) from exc
    except OSError as exc:
        raise _RemoteFinalizationFailure(
            f"failed to prepare local {label} download staging: {exc}",
            GdsExportReason.PUBLICATION_ERROR,
        ) from exc

    try:
        response = getattr(client, "download_file")(
            remote_path.as_posix(),
            local_path,
            timeout=budget.timeout(finalizing=True),
        )
        warnings.extend(_response_warnings(response))
        response_errors, response_status, _response_output = response_fields(
            response
        )
    except Exception as exc:
        raise _RemoteFinalizationFailure(
            f"failed to download remote XStream {label}: {exc}",
            GdsExportReason.TRANSPORT_ERROR,
            timed_out=_exception_timed_out(exc),
        ) from exc
    if not _response_succeeded(response_status):
        detail = "; ".join(response_errors) or (
            "download returned non-success status without errors: "
            f"{response_status!r}"
        )
        raise _RemoteFinalizationFailure(
            f"failed to download remote XStream {label}: {detail}",
            GdsExportReason.TRANSPORT_ERROR,
        )
    if budget.remaining(finalizing=True) <= 0.0:
        raise _RemoteFinalizationFailure(
            f"export time budget exhausted after remote XStream {label} download",
            GdsExportReason.TRANSPORT_ERROR,
            timed_out=True,
        )


def _read_downloaded_file(path: Path, *, label: str) -> bytes:
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"downloaded {label} is not a regular file: {path}")
        return path.read_bytes()
    except OSError as exc:
        raise _RemoteFinalizationFailure(
            f"failed to validate downloaded remote XStream {label}: {exc}",
            GdsExportReason.TRANSPORT_ERROR,
        ) from exc


def _stream_file_sha256(path: Path, budget: _Budget) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as artifact:
        metadata = os.fstat(artifact.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"downloaded GDS is not a regular file: {path}")
        while True:
            budget.timeout(finalizing=True)
            chunk = artifact.read(_REMOTE_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _cleanup_local_download_temp(
    path: Path,
    budget: _Budget,
    warnings: list[str],
) -> None:
    after_deadline = budget.remaining(finalizing=True) <= 0.0
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        warnings.append(f"local download temp retained: {path}: {exc}")
    else:
        if after_deadline:
            warnings.append(
                f"local download temp removed after export deadline: {path}"
            )


def _stabilize_remote_log(
    client: object,
    runner: object,
    inputs: _ExportInputs,
    paths: _RemoteExportPaths,
    budget: _Budget,
    initial_observation: _ArtifactObservation,
    warnings: list[str],
) -> _RemoteLogFinalization:
    observation = initial_observation
    local_path: Path | None = None
    changed_before_publication = False

    if observation.log_present and observation.log_digest is None:
        try:
            observation = _observe_remote_artifacts(
                runner,
                paths,
                budget,
                finalizing=True,
            )
        except Exception as exc:
            return _RemoteLogFinalization(
                observation=observation,
                log=None,
                local_path=None,
                stable=False,
                error=f"failed to fingerprint remote XStream log: {exc}",
                reason=GdsExportReason.TRANSPORT_ERROR,
                timed_out=_exception_timed_out(exc),
            )

    for _ in range(_MAX_FINAL_LOG_REFRESHES):
        if not observation.log_present:
            return _RemoteLogFinalization(
                observation=observation,
                log=None,
                local_path=local_path,
                stable=True,
            )

        temporary = _remote_download_temp_path(inputs.log_path)
        try:
            _download_remote_file(
                client,
                paths.log,
                temporary,
                budget,
                warnings,
                label="log",
            )
            log_bytes = _read_downloaded_file(temporary, label="log")
            budget.timeout(finalizing=True)
            downloaded_log_digest = hashlib.sha256(log_bytes).hexdigest()
            refreshed = _observe_remote_artifacts(
                runner,
                paths,
                budget,
                finalizing=True,
            )
        except _RemoteFinalizationFailure as exc:
            _cleanup_local_download_temp(temporary, budget, warnings)
            return _RemoteLogFinalization(
                observation=observation,
                log=None,
                local_path=local_path,
                stable=False,
                error=str(exc),
                reason=exc.reason,
                timed_out=exc.timed_out,
            )
        except Exception as exc:
            _cleanup_local_download_temp(temporary, budget, warnings)
            return _RemoteLogFinalization(
                observation=observation,
                log=None,
                local_path=local_path,
                stable=False,
                error=f"failed to recheck remote XStream log: {exc}",
                reason=GdsExportReason.TRANSPORT_ERROR,
                timed_out=_exception_timed_out(exc),
            )

        if (
            len(log_bytes) != observation.log_size
            or downloaded_log_digest != observation.log_digest
            or _remote_log_fingerprint(refreshed)
            != _remote_log_fingerprint(observation)
        ):
            if (
                _remote_log_fingerprint(refreshed)
                != _remote_log_fingerprint(observation)
            ):
                local_path = None
            observation = refreshed
            _cleanup_local_download_temp(temporary, budget, warnings)
            continue

        log_text = log_bytes.decode("utf-8", errors="replace")
        parsed_log: XStreamLogResult | None = None
        validated_observation = refreshed

        def validate_remote_log_snapshot() -> None:
            nonlocal validated_observation
            try:
                latest = _observe_remote_artifacts(
                    runner,
                    paths,
                    budget,
                    finalizing=True,
                )
                budget.timeout(finalizing=True)
            except Exception as exc:
                raise _RemoteFinalizationFailure(
                    "failed to validate remote XStream log before "
                    f"publication: {exc}",
                    GdsExportReason.TRANSPORT_ERROR,
                    timed_out=_exception_timed_out(exc),
                ) from exc
            validated_observation = latest
            if (
                _remote_log_fingerprint(latest)
                != _remote_log_fingerprint(observation)
            ):
                raise _RemoteSnapshotChanged(latest)

        try:
            parsed_log = parse_xstream_log(log_text)
            budget.timeout(finalizing=True)
            _publish_file(
                temporary,
                inputs.log_path,
                validator=validate_remote_log_snapshot,
            )
            local_path = inputs.log_path
        except _RemoteSnapshotChanged as exc:
            changed_before_publication = True
            local_path = None
            observation = exc.observation
            _cleanup_local_download_temp(temporary, budget, warnings)
            continue
        except _RemoteFinalizationFailure as exc:
            _cleanup_local_download_temp(temporary, budget, warnings)
            return _RemoteLogFinalization(
                observation=observation,
                log=parsed_log,
                local_path=None,
                stable=False,
                error=str(exc),
                reason=exc.reason,
                timed_out=exc.timed_out,
            )
        except _BudgetExpired as exc:
            _cleanup_local_download_temp(temporary, budget, warnings)
            return _RemoteLogFinalization(
                observation=observation,
                log=parsed_log,
                local_path=None,
                stable=False,
                error=str(exc),
                reason=GdsExportReason.PUBLICATION_ERROR,
                timed_out=True,
            )
        except (OSError, ValueError, TypeError) as exc:
            _cleanup_local_download_temp(temporary, budget, warnings)
            return _RemoteLogFinalization(
                observation=observation,
                log=parsed_log,
                local_path=None,
                stable=False,
                error=f"failed to publish remote XStream log: {exc}",
                reason=GdsExportReason.PUBLICATION_ERROR,
            )

        _cleanup_local_download_temp(temporary, budget, warnings)
        if budget.remaining(finalizing=True) <= 0.0:
            return _RemoteLogFinalization(
                observation=validated_observation,
                log=parsed_log,
                local_path=local_path,
                stable=False,
                error=(
                    "export time budget exhausted after remote XStream "
                    "log publication"
                ),
                reason=GdsExportReason.PUBLICATION_ERROR,
                timed_out=True,
            )
        return _RemoteLogFinalization(
            observation=validated_observation,
            log=parsed_log,
            local_path=local_path,
            stable=True,
        )

    if changed_before_publication:
        error = "remote XStream log did not stabilize before publication"
        reason = GdsExportReason.PUBLICATION_ERROR
    else:
        error = "remote XStream log did not stabilize during download"
        reason = GdsExportReason.TRANSPORT_ERROR
    return _RemoteLogFinalization(
        observation=observation,
        log=None,
        local_path=local_path,
        stable=False,
        error=error,
        reason=reason,
    )


def _finalize_remote_export(
    client: object,
    runner: object,
    inputs: _ExportInputs,
    paths: _RemoteExportPaths,
    budget: _Budget,
    outcome: _PollOutcome,
    *,
    skill_errors: tuple[str, ...],
    cleanup_failures: tuple[str, ...],
    launch_indeterminate: bool,
    warnings: list[str],
) -> GdsExportResult:
    result_warnings = list(warnings)
    final_observation = outcome.observation
    final_log: XStreamLogResult | None = None
    local_log_path: Path | None = None
    local_gds_path: Path | None = None
    operational_error = outcome.staging_error
    operational_reason = (
        GdsExportReason.TRANSPORT_ERROR
        if operational_error is not None
        else None
    )
    operational_timed_out = (
        outcome.deadline_expired and operational_error is not None
    )
    diagnostic_log_ready = False

    launch_is_blocked = bool(cleanup_failures) or bool(
        skill_errors and not _is_indeterminate_skill_timeout(skill_errors)
    )
    if operational_error is None and not launch_is_blocked:
        try:
            final_observation = _observe_remote_artifacts(
                runner,
                paths,
                budget,
                finalizing=True,
            )
        except Exception as exc:
            operational_error = (
                f"failed to refresh remote XStream artifacts: {exc}"
            )
            operational_reason = GdsExportReason.TRANSPORT_ERROR
            operational_timed_out = _exception_timed_out(exc)

    if operational_error is None:
        log_finalization = _stabilize_remote_log(
            client,
            runner,
            inputs,
            paths,
            budget,
            final_observation,
            result_warnings,
        )
        final_observation = log_finalization.observation
        final_log = log_finalization.log
        local_log_path = log_finalization.local_path
        diagnostic_log_ready = bool(
            log_finalization.stable
            and final_log is not None
            and local_log_path is not None
        )
        if log_finalization.error is not None:
            operational_error = log_finalization.error
            operational_reason = log_finalization.reason
            operational_timed_out = log_finalization.timed_out

    gds_allowed = (
        operational_error is None
        and diagnostic_log_ready
        and not cleanup_failures
        and (not skill_errors or _is_indeterminate_skill_timeout(skill_errors))
        and _has_valid_zero_error_completion(final_log)
    )
    gds_download_mismatch = False
    if gds_allowed:
        for _ in range(_MAX_FINAL_LOG_REFRESHES):
            if (
                not final_observation.gds_present
                or final_observation.gds_size <= 0
            ):
                break
            expected = final_observation
            temporary = _remote_download_temp_path(inputs.output_path)
            try:
                _download_remote_file(
                    client,
                    paths.gds,
                    temporary,
                    budget,
                    result_warnings,
                    label="GDS",
                )
                downloaded_gds_size, downloaded_gds_digest = (
                    _stream_file_sha256(temporary, budget)
                )
                refreshed = _observe_remote_artifacts(
                    runner,
                    paths,
                    budget,
                    finalizing=True,
                )
            except _RemoteFinalizationFailure as exc:
                _cleanup_local_download_temp(temporary, budget, result_warnings)
                operational_error = str(exc)
                operational_reason = exc.reason
                operational_timed_out = exc.timed_out
                break
            except Exception as exc:
                _cleanup_local_download_temp(temporary, budget, result_warnings)
                operational_error = (
                    f"failed to verify downloaded remote XStream GDS: {exc}"
                )
                operational_reason = GdsExportReason.TRANSPORT_ERROR
                operational_timed_out = _exception_timed_out(exc)
                break

            if (
                _remote_log_fingerprint(refreshed)
                != _remote_log_fingerprint(expected)
            ):
                _cleanup_local_download_temp(temporary, budget, result_warnings)
                log_finalization = _stabilize_remote_log(
                    client,
                    runner,
                    inputs,
                    paths,
                    budget,
                    refreshed,
                    result_warnings,
                )
                final_observation = log_finalization.observation
                final_log = log_finalization.log
                local_log_path = log_finalization.local_path
                diagnostic_log_ready = bool(
                    log_finalization.stable
                    and final_log is not None
                    and local_log_path is not None
                )
                if log_finalization.error is not None:
                    operational_error = log_finalization.error
                    operational_reason = log_finalization.reason
                    operational_timed_out = log_finalization.timed_out
                    break
                if not _has_valid_zero_error_completion(final_log):
                    break
                continue

            if (
                downloaded_gds_size != expected.gds_size
                or downloaded_gds_digest != expected.gds_digest
            ):
                gds_download_mismatch = True
                final_observation = refreshed
                _cleanup_local_download_temp(temporary, budget, result_warnings)
                continue

            if _remote_gds_fingerprint(refreshed) != _remote_gds_fingerprint(
                expected
            ):
                final_observation = refreshed
                _cleanup_local_download_temp(temporary, budget, result_warnings)
                continue

            validated_observation = refreshed

            def validate_remote_snapshot() -> None:
                nonlocal validated_observation
                try:
                    latest = _observe_remote_artifacts(
                        runner,
                        paths,
                        budget,
                        finalizing=True,
                    )
                    budget.timeout(finalizing=True)
                except Exception as exc:
                    raise _RemoteFinalizationFailure(
                        "failed to validate remote XStream artifacts before "
                        f"GDS publication: {exc}",
                        GdsExportReason.TRANSPORT_ERROR,
                        timed_out=_exception_timed_out(exc),
                    ) from exc
                validated_observation = latest
                if (
                    _remote_log_fingerprint(latest)
                    != _remote_log_fingerprint(expected)
                    or _remote_gds_fingerprint(latest)
                    != _remote_gds_fingerprint(expected)
                ):
                    raise _RemoteSnapshotChanged(latest)

            try:
                budget.timeout(finalizing=True)
                _publish_file(
                    temporary,
                    inputs.output_path,
                    validator=validate_remote_snapshot,
                )
                local_gds_path = inputs.output_path
                final_observation = validated_observation
                if budget.remaining(finalizing=True) <= 0.0:
                    result_warnings.append(
                        "GDS publication completed after the export deadline; "
                        "remote staging retained"
                    )
            except _RemoteSnapshotChanged as exc:
                final_observation = exc.observation
                _cleanup_local_download_temp(temporary, budget, result_warnings)
                if (
                    _remote_log_fingerprint(final_observation)
                    != _remote_log_fingerprint(expected)
                ):
                    log_finalization = _stabilize_remote_log(
                        client,
                        runner,
                        inputs,
                        paths,
                        budget,
                        final_observation,
                        result_warnings,
                    )
                    final_observation = log_finalization.observation
                    final_log = log_finalization.log
                    local_log_path = log_finalization.local_path
                    diagnostic_log_ready = bool(
                        log_finalization.stable
                        and final_log is not None
                        and local_log_path is not None
                    )
                    if log_finalization.error is not None:
                        operational_error = log_finalization.error
                        operational_reason = log_finalization.reason
                        operational_timed_out = log_finalization.timed_out
                        break
                    if not _has_valid_zero_error_completion(final_log):
                        break
                continue
            except _RemoteFinalizationFailure as exc:
                operational_error = str(exc)
                operational_reason = exc.reason
                operational_timed_out = exc.timed_out
            except _BudgetExpired as exc:
                operational_error = str(exc)
                operational_reason = GdsExportReason.PUBLICATION_ERROR
                operational_timed_out = True
            except (OSError, ValueError, TypeError) as exc:
                operational_error = f"failed to publish remote XStream GDS: {exc}"
                operational_reason = GdsExportReason.PUBLICATION_ERROR
            finally:
                _cleanup_local_download_temp(
                    temporary,
                    budget,
                    result_warnings,
                )
            break
        else:
            if gds_download_mismatch:
                operational_error = (
                    "downloaded remote XStream GDS content did not stabilize"
                )
                operational_reason = GdsExportReason.TRANSPORT_ERROR
            else:
                operational_error = (
                    "remote XStream artifacts did not stabilize before GDS "
                    "publication"
                )
                operational_reason = GdsExportReason.PUBLICATION_ERROR

    final_outcome = _PollOutcome(
        observation=final_observation,
        log=final_log,
        saw_evidence=(
            outcome.saw_evidence
            or _observation_has_evidence(final_observation, final_log)
        ),
        deadline_expired=outcome.deadline_expired,
    )
    errors = _result_errors(
        final_outcome,
        skill_errors=skill_errors,
        cleanup_failures=cleanup_failures,
    )
    if final_log is not None:
        result_warnings.extend(final_log.warnings)

    if operational_error is not None:
        errors.append(operational_error)
        status = ExecutionStatus.ERROR
        reason = operational_reason or GdsExportReason.TRANSPORT_ERROR
        timed_out = (
            operational_timed_out
            or budget.remaining(finalizing=True) <= 0.0
        )
    else:
        status, reason, timed_out = _classify_export(
            cleanup_failures=cleanup_failures,
            log=final_log,
            skill_errors=skill_errors,
            launch_indeterminate=launch_indeterminate,
            saw_evidence=final_outcome.saw_evidence,
            gds_present=final_observation.gds_present,
            gds_size=final_observation.gds_size,
            gds_published=local_gds_path is not None,
            deadline_expired=outcome.deadline_expired,
        )

    if final_observation.gds_present and local_gds_path is None:
        _discard_remote_gds(runner, paths, budget, result_warnings)

    remote_files_retained: bool | None = True
    should_cleanup = (
        inputs.cleanup_policy == "success" and status == ExecutionStatus.SUCCESS
    ) or (
        inputs.cleanup_policy == "always" and diagnostic_log_ready
    )
    if should_cleanup:
        remote_files_retained = _cleanup_remote_run(
            runner,
            paths,
            budget,
            result_warnings,
        )

    return GdsExportResult(
        status=status,
        reason=reason,
        timed_out=timed_out,
        library=inputs.library,
        cell=inputs.cell,
        view=inputs.view,
        execution_time=budget.elapsed(),
        local_gds_path=local_gds_path,
        local_log_path=local_log_path,
        log_result=final_log,
        errors=tuple(errors),
        warnings=tuple(result_warnings),
        remote_run_dir=paths.run_dir.as_posix(),
        remote_files_retained=remote_files_retained,
    )


def _remote_paths_are_owned(paths: _RemoteExportPaths) -> bool:
    return bool(
        paths.owned_root.is_absolute()
        and ".." not in paths.owned_root.parts
        and paths.run_dir.parent == paths.owned_root
        and re.fullmatch(r"[0-9a-f]{32}", paths.run_dir.name)
        and paths.gds == paths.run_dir / "output.gds"
        and paths.log == paths.run_dir / "xstream.log"
        and paths.stream_map == paths.run_dir / "stream.map"
        and "\\" not in paths.run_dir.as_posix()
    )


def _discard_remote_gds(
    runner: object,
    paths: _RemoteExportPaths,
    budget: _Budget,
    warnings: list[str],
) -> None:
    if not _remote_paths_are_owned(paths):
        warnings.append("refused to discard GDS outside owned XStream run")
        return
    if budget.remaining(finalizing=True) <= 0.0:
        warnings.append(
            "remote GDS discard skipped because the export deadline expired"
        )
        return
    try:
        result = getattr(runner, "run_command")(
            _remote_delete_command(paths, remove_run=False),
            timeout=budget.timeout(finalizing=True),
        )
        returncode, stdout, stderr = _command_result_fields(result)
    except Exception as exc:
        warnings.append(f"failed to discard unvalidated remote GDS: {exc}")
        return
    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or (
            f"exit status {returncode}"
        )
        warnings.append(f"failed to discard unvalidated remote GDS: {detail}")


_REMOTE_DISCONNECT_FRAGMENTS = (
    "connection reset",
    "connection closed",
    "broken pipe",
    "connection timed out",
    "no route to host",
    "could not resolve hostname",
    "kex_exchange_identification",
)


def _cleanup_remote_run(
    runner: object,
    paths: _RemoteExportPaths,
    budget: _Budget,
    warnings: list[str],
) -> bool | None:
    if not _remote_paths_are_owned(paths):
        warnings.append("refused to clean unowned remote XStream path")
        return True
    if budget.remaining(finalizing=True) <= 0.0:
        warnings.append(
            "remote cleanup skipped because the export deadline expired"
        )
        return True
    try:
        result = getattr(runner, "run_command")(
            _remote_delete_command(paths, remove_run=True),
            timeout=budget.timeout(finalizing=True),
        )
        returncode, stdout, stderr = _command_result_fields(result)
    except _BudgetExpired:
        warnings.append(
            "remote cleanup skipped because the export deadline expired"
        )
        return True
    except PermissionError as exc:
        warnings.append(f"remote XStream cleanup failed: {exc}")
        return True
    except Exception as exc:
        warnings.append(f"remote XStream cleanup could not be verified: {exc}")
        return None
    if returncode == 0:
        if budget.remaining(finalizing=True) <= 0.0:
            warnings.append(
                "remote XStream cleanup completed after the export deadline"
            )
        return False

    detail = stderr.strip() or stdout.strip() or f"exit status {returncode}"
    if returncode == 255 or any(
        fragment in detail.lower()
        for fragment in _REMOTE_DISCONNECT_FRAGMENTS
    ):
        warnings.append(f"remote XStream cleanup could not be verified: {detail}")
        return None
    warnings.append(f"remote XStream cleanup failed: {detail}")
    return True


def _export_gds_local(
    client: object,
    inputs: _ExportInputs,
    budget: _Budget,
    recovery_hook: Callable[[], object] | None,
) -> GdsExportResult:
    run_dir = inputs.output_path.parent / (
        f".{inputs.output_path.name}.xstream-{uuid.uuid4().hex}"
    )
    paths = _ExportPaths(
        run_dir=run_dir,
        gds=run_dir / "output.gds",
        log=run_dir / "xstream.log",
        snapshot_log=run_dir / "xstream-snapshot.log",
        diagnostic_log=run_dir / "bridge-diagnostic.log",
    )
    run_created = False
    try:
        parents = tuple(
            dict.fromkeys((inputs.output_path.parent, inputs.log_path.parent))
        )
        for parent in parents:
            budget.timeout(finalizing=False)
            parent.mkdir(parents=True, exist_ok=True)
        budget.timeout(finalizing=False)
        paths.run_dir.mkdir(parents=False, exist_ok=False)
        run_created = True
    except (_BudgetExpired, OSError) as exc:
        return _staging_error_result(
            inputs,
            budget,
            f"failed to create local XStream staging: {exc}",
            local_run_dir=paths.run_dir if run_created else None,
            timed_out=isinstance(exc, _BudgetExpired),
        )

    request = XStreamExportRequest(
        library=inputs.library,
        top_cell=inputs.cell,
        view=inputs.view,
        stream_file=str(paths.gds),
        layer_map=str(inputs.stream_map),
        log_file=str(paths.log),
        run_dir=str(paths.run_dir),
    )
    skill_errors: tuple[str, ...] = ()
    cleanup_failures: tuple[str, ...] = ()
    launch_indeterminate = False
    should_poll = False
    warnings: list[str] = []
    try:
        skill_code = xstream_export_gds_skill(request)
        effective_skill_timeout = budget.timeout(
            finalizing=False,
            cap=inputs.skill_timeout,
        )
        response = getattr(client, "execute_skill")(
            skill_code,
            timeout=effective_skill_timeout,
        )
        warnings.extend(_response_warnings(response))
    except _BudgetExpired as exc:
        outcome = _PollOutcome(
            observation=_ArtifactObservation(),
            log=None,
            saw_evidence=False,
            deadline_expired=True,
            staging_error=str(exc),
        )
        return _finalize_local_export(
            inputs,
            paths,
            budget,
            outcome,
            skill_errors=(),
            cleanup_failures=(),
            launch_indeterminate=False,
            warnings=warnings,
        )
    except Exception as exc:
        skill_errors = (f"SKILL execution failed: {exc}",)
    else:
        try:
            response_errors, response_status, response_output = response_fields(
                response
            )
        except Exception as exc:
            skill_errors = (f"failed to normalize SKILL response: {exc}",)
        else:
            skill_errors = tuple(response_errors)
            response_succeeded = _response_succeeded(response_status)
            if not response_succeeded and not skill_errors:
                skill_errors = (
                    "SKILL request returned non-success status without errors: "
                    f"{response_status!r}",
                )

            if not response_succeeded or skill_errors:
                launch_indeterminate = _is_indeterminate_skill_timeout(
                    skill_errors
                )
                should_poll = launch_indeterminate
            else:
                try:
                    request_response = _parse_xstream_request_response(
                        response_output
                    )
                except ValueError as exc:
                    skill_errors = (str(exc),)
                else:
                    cleanup_failures = request_response.cleanup_failures
                    if request_response.state == "failed":
                        skill_errors = (
                            request_response.body_error
                            or "XStream request body failed",
                        )
                    elif not cleanup_failures:
                        should_poll = True

    outcome = _poll_local_artifacts(
        paths,
        budget,
        inputs.poll_interval,
        should_poll=should_poll,
        launch_indeterminate=launch_indeterminate,
        recovery_hook=recovery_hook,
        warnings=warnings,
    )
    return _finalize_local_export(
        inputs,
        paths,
        budget,
        outcome,
        skill_errors=skill_errors,
        cleanup_failures=cleanup_failures,
        launch_indeterminate=launch_indeterminate,
        warnings=warnings,
    )


def _response_succeeded(status: object) -> bool:
    return status in (ExecutionStatus.SUCCESS, ExecutionStatus.SUCCESS.value)


def _response_warnings(response: object) -> tuple[str, ...]:
    if isinstance(response, dict):
        nested = (
            response.get("result")
            if isinstance(response.get("result"), dict)
            else {}
        )
        value = response.get("warnings") or nested.get("warnings")
    else:
        value = getattr(response, "warnings", None)
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(warning) for warning in value)
    return (str(value),)


def _poll_local_artifacts(
    paths: _ExportPaths,
    budget: _Budget,
    poll_interval: float,
    *,
    should_poll: bool,
    launch_indeterminate: bool,
    recovery_hook: Callable[[], object] | None,
    warnings: list[str],
) -> _PollOutcome:
    observation = _ArtifactObservation()
    log: XStreamLogResult | None = None
    saw_evidence = False
    deadline_expired = False
    recovery_attempted = False

    while True:
        if budget.remaining(finalizing=True) <= 0.0:
            deadline_expired = True
            break
        try:
            observation = _observe_local_artifacts(paths)
            log = (
                parse_xstream_log(observation.log_text)
                if observation.log_present
                else None
            )
        except (OSError, ValueError, TypeError) as exc:
            return _PollOutcome(
                observation=observation,
                log=log,
                saw_evidence=saw_evidence,
                deadline_expired=budget.remaining(finalizing=False) <= 0.0,
                staging_error=f"failed to observe local XStream artifacts: {exc}",
            )

        progress = _observation_has_evidence(observation, log)
        saw_evidence = saw_evidence or progress
        if (
            launch_indeterminate
            and progress
            and not recovery_attempted
            and recovery_hook is not None
            and budget.remaining(finalizing=False) > 0.0
        ):
            recovery_attempted = True
            try:
                recovery_hook()
            except Exception as exc:
                warnings.append(f"recovery hook failed: {exc}")

        if _observation_is_terminal(observation, log):
            deadline_expired = budget.remaining(finalizing=False) <= 0.0
            break
        if not should_poll:
            deadline_expired = budget.remaining(finalizing=False) <= 0.0
            break

        remaining = budget.remaining(finalizing=False)
        if remaining <= 0.0:
            deadline_expired = True
            break
        delay = min(poll_interval, remaining)
        if delay <= 0.0:
            deadline_expired = True
            break
        try:
            _SLEEP(delay)
        except (OSError, OverflowError) as exc:
            return _PollOutcome(
                observation=observation,
                log=log,
                saw_evidence=saw_evidence,
                deadline_expired=budget.remaining(finalizing=False) <= 0.0,
                staging_error=f"failed while polling local artifacts: {exc}",
            )

    return _PollOutcome(
        observation=observation,
        log=log,
        saw_evidence=saw_evidence,
        deadline_expired=deadline_expired,
    )


def _observe_local_artifacts(paths: _ExportPaths) -> _ArtifactObservation:
    log_present, log_bytes, log_text = _read_local_log_snapshot(paths.log)
    gds_present, gds_size = _local_file_size(paths.gds)
    return _ArtifactObservation(
        log_present=log_present,
        log_size=len(log_bytes),
        log_bytes=log_bytes,
        log_text=log_text,
        gds_present=gds_present,
        gds_size=gds_size,
    )


def _read_local_log_snapshot(path: Path) -> tuple[bool, bytes, str]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return False, b"", ""
    except OSError as exc:
        raise OSError(f"failed to read {path}: {exc}") from exc
    return True, data, data.decode("utf-8", errors="replace")


def _local_file_size(path: Path) -> tuple[bool, int]:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return False, 0
    except OSError as exc:
        raise OSError(f"failed to stat {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"staged artifact is not a regular file: {path}")
    return True, metadata.st_size


def _observation_has_evidence(
    observation: _ArtifactObservation,
    log: XStreamLogResult | None,
) -> bool:
    return (
        observation.log_size > 0
        or observation.gds_size > 0
        or (log is not None and (log.completed or bool(log.terminal_failures)))
    )


def _observation_is_terminal(
    observation: _ArtifactObservation,
    log: XStreamLogResult | None,
) -> bool:
    if log is None:
        return False
    if log.terminal_failures:
        return True
    if log.completed and (
        log.parse_errors
        or log.error_count is None
        or log.warning_count is None
    ):
        return True
    if log.completed and log.error_count != 0:
        return True
    return (
        _has_valid_zero_error_completion(log)
        and observation.gds_present
        and observation.gds_size > 0
    )


def _has_valid_zero_error_completion(log: XStreamLogResult | None) -> bool:
    return bool(
        log is not None
        and log.completed
        and log.error_count == 0
        and log.warning_count is not None
        and not log.terminal_failures
        and not log.parse_errors
    )


def _refresh_local_log_outcome(
    paths: _ExportPaths,
    outcome: _PollOutcome,
) -> _PollOutcome:
    log_present, log_bytes, log_text = _read_local_log_snapshot(paths.log)
    observation = _ArtifactObservation(
        log_present=log_present,
        log_size=len(log_bytes),
        log_bytes=log_bytes,
        log_text=log_text,
        gds_present=outcome.observation.gds_present,
        gds_size=outcome.observation.gds_size,
    )
    log = parse_xstream_log(log_text) if log_present else None
    return _PollOutcome(
        observation=observation,
        log=log,
        saw_evidence=(
            outcome.saw_evidence
            or _observation_has_evidence(observation, log)
        ),
        deadline_expired=outcome.deadline_expired,
        staging_error=outcome.staging_error,
    )


def _publish_local_log_snapshot(
    inputs: _ExportInputs,
    paths: _ExportPaths,
    budget: _Budget,
    outcome: _PollOutcome,
    errors: list[str],
) -> tuple[Path | None, str | None, bool]:
    log_source: Path | None = None
    if outcome.observation.log_present and outcome.observation.log_size > 0:
        try:
            budget.timeout(finalizing=True)
            paths.snapshot_log.write_bytes(outcome.observation.log_bytes)
            log_source = paths.snapshot_log
        except _BudgetExpired as exc:
            return None, str(exc), True
        except OSError as exc:
            return None, f"failed to stage XStream log snapshot: {exc}", False
    else:
        try:
            budget.timeout(finalizing=True)
            paths.diagnostic_log.write_text(
                _diagnostic_log_text(inputs, errors),
                encoding="utf-8",
            )
            log_source = paths.diagnostic_log
        except _BudgetExpired as exc:
            return None, str(exc), True
        except (OSError, UnicodeError) as exc:
            return None, f"failed to recover diagnostic log: {exc}", False

    try:
        budget.timeout(finalizing=True)
        _publish_file(log_source, inputs.log_path)
        if budget.remaining(finalizing=True) <= 0.0:
            return (
                inputs.log_path,
                "export time budget exhausted after XStream log publication",
                True,
            )
    except _BudgetExpired as exc:
        return None, str(exc), True
    except OSError as exc:
        return None, f"failed to publish XStream log: {exc}", False
    return inputs.log_path, None, False


def _finalize_local_export(
    inputs: _ExportInputs,
    paths: _ExportPaths,
    budget: _Budget,
    outcome: _PollOutcome,
    *,
    skill_errors: tuple[str, ...],
    cleanup_failures: tuple[str, ...],
    launch_indeterminate: bool,
    warnings: list[str],
) -> GdsExportResult:
    final_outcome = outcome
    publication_error: str | None = None
    publication_timed_out = False
    should_refresh_log = outcome.staging_error is None
    if should_refresh_log:
        try:
            budget.timeout(finalizing=True)
            final_outcome = _refresh_local_log_outcome(paths, outcome)
            if budget.remaining(finalizing=True) <= 0.0:
                publication_error = (
                    "export time budget exhausted while snapshotting XStream log"
                )
                publication_timed_out = True
        except _BudgetExpired as exc:
            publication_error = str(exc)
            publication_timed_out = True
        except (OSError, ValueError, TypeError) as exc:
            publication_error = f"failed to snapshot XStream log: {exc}"

    errors = _result_errors(
        final_outcome,
        skill_errors=skill_errors,
        cleanup_failures=cleanup_failures,
    )
    result_warnings = list(warnings)
    if final_outcome.log is not None:
        result_warnings.extend(final_outcome.log.warnings)

    local_log_path: Path | None = None
    local_gds_path: Path | None = None
    final_gds_present = final_outcome.observation.gds_present
    final_gds_size = final_outcome.observation.gds_size

    if publication_error is None:
        (
            local_log_path,
            publication_error,
            publication_timed_out,
        ) = _publish_local_log_snapshot(
            inputs,
            paths,
            budget,
            final_outcome,
            errors,
        )

    if publication_error is None and local_log_path is not None:
        for _ in range(_MAX_FINAL_LOG_REFRESHES):
            try:
                budget.timeout(finalizing=True)
                refreshed_outcome = _refresh_local_log_outcome(
                    paths,
                    final_outcome,
                )
            except _BudgetExpired as exc:
                publication_error = str(exc)
                publication_timed_out = True
                break
            except (OSError, ValueError, TypeError) as exc:
                publication_error = (
                    "failed to stabilize XStream log before final "
                    f"publication: {exc}"
                )
                break

            previous_snapshot = (
                final_outcome.observation.log_present,
                final_outcome.observation.log_bytes,
            )
            refreshed_snapshot = (
                refreshed_outcome.observation.log_present,
                refreshed_outcome.observation.log_bytes,
            )
            if refreshed_snapshot == previous_snapshot:
                break

            final_outcome = refreshed_outcome
            errors = _result_errors(
                final_outcome,
                skill_errors=skill_errors,
                cleanup_failures=cleanup_failures,
            )
            result_warnings = list(warnings)
            if final_outcome.log is not None:
                result_warnings.extend(final_outcome.log.warnings)
            (
                local_log_path,
                publication_error,
                refresh_timed_out,
            ) = _publish_local_log_snapshot(
                inputs,
                paths,
                budget,
                final_outcome,
                errors,
            )
            publication_timed_out = (
                publication_timed_out or refresh_timed_out
            )
            if publication_error is not None:
                break
        else:
            publication_error = (
                "XStream log did not stabilize before final publication"
            )

    gds_publication_allowed = (
        final_outcome.staging_error is None
        and publication_error is None
        and local_log_path is not None
        and not cleanup_failures
        and (not skill_errors or _is_indeterminate_skill_timeout(skill_errors))
        and _has_valid_zero_error_completion(final_outcome.log)
    )
    gds_revalidated = False
    if gds_publication_allowed:
        source_snapshot_error: str | None = None
        for _ in range(_MAX_FINAL_LOG_REFRESHES):
            source_snapshot_error = None
            try:
                budget.timeout(finalizing=True)
                refreshed_outcome = _refresh_local_log_outcome(
                    paths,
                    final_outcome,
                )
            except _BudgetExpired as exc:
                publication_error = str(exc)
                publication_timed_out = True
                break
            except (OSError, ValueError, TypeError) as exc:
                publication_error = (
                    "failed to recheck XStream log before GDS publication: "
                    f"{exc}"
                )
                break

            previous_snapshot = (
                final_outcome.observation.log_present,
                final_outcome.observation.log_bytes,
            )
            refreshed_snapshot = (
                refreshed_outcome.observation.log_present,
                refreshed_outcome.observation.log_bytes,
            )
            if refreshed_snapshot == previous_snapshot:
                try:
                    budget.timeout(finalizing=True)
                    final_gds_present, final_gds_size = _local_file_size(
                        paths.gds
                    )
                except _BudgetExpired as exc:
                    publication_error = str(exc)
                    publication_timed_out = True
                    break
                except OSError as exc:
                    publication_error = (
                        f"failed to revalidate staged GDS: {exc}"
                    )
                    break

                try:
                    budget.timeout(finalizing=True)
                    refreshed_outcome = _refresh_local_log_outcome(
                        paths,
                        final_outcome,
                    )
                except _BudgetExpired as exc:
                    publication_error = str(exc)
                    publication_timed_out = True
                    break
                except (OSError, ValueError, TypeError) as exc:
                    publication_error = (
                        "failed to recheck XStream log after GDS "
                        f"revalidation: {exc}"
                    )
                    break

                refreshed_snapshot = (
                    refreshed_outcome.observation.log_present,
                    refreshed_outcome.observation.log_bytes,
                )
                if refreshed_snapshot == previous_snapshot:
                    if not final_gds_present or final_gds_size <= 0:
                        gds_revalidated = True
                        break

                    def validate_log_before_replace() -> None:
                        budget.timeout(finalizing=True)
                        latest_outcome = _refresh_local_log_outcome(
                            paths,
                            final_outcome,
                        )
                        budget.timeout(finalizing=True)
                        latest_snapshot = (
                            latest_outcome.observation.log_present,
                            latest_outcome.observation.log_bytes,
                        )
                        if latest_snapshot != previous_snapshot:
                            raise _LogSnapshotChanged(latest_outcome)

                    try:
                        budget.timeout(finalizing=True)
                        _publish_file(
                            paths.gds,
                            inputs.output_path,
                            validator=validate_log_before_replace,
                        )
                        local_gds_path = inputs.output_path
                        gds_revalidated = True
                        if budget.remaining(finalizing=True) <= 0.0:
                            result_warnings.append(
                                "GDS publication completed after the export "
                                "deadline; local staging retained"
                            )
                        break
                    except _SourceSnapshotChanged as exc:
                        source_snapshot_error = str(exc)
                        continue
                    except _LogSnapshotChanged as exc:
                        refreshed_outcome = exc.outcome
                    except _EmptyPublicationFileError:
                        final_gds_present = True
                        final_gds_size = 0
                        gds_revalidated = True
                        break
                    except _BudgetExpired as exc:
                        publication_error = str(exc)
                        publication_timed_out = True
                        break
                    except (OSError, ValueError, TypeError) as exc:
                        publication_error = f"failed to publish GDS: {exc}"
                        break

            final_outcome = refreshed_outcome
            errors = _result_errors(
                final_outcome,
                skill_errors=skill_errors,
                cleanup_failures=cleanup_failures,
            )
            result_warnings = list(warnings)
            if final_outcome.log is not None:
                result_warnings.extend(final_outcome.log.warnings)
            (
                local_log_path,
                publication_error,
                refresh_timed_out,
            ) = _publish_local_log_snapshot(
                inputs,
                paths,
                budget,
                final_outcome,
                errors,
            )
            publication_timed_out = (
                publication_timed_out or refresh_timed_out
            )
            if (
                publication_error is not None
                or not _has_valid_zero_error_completion(final_outcome.log)
            ):
                break
        else:
            if source_snapshot_error is None:
                publication_error = (
                    "XStream log did not stabilize before GDS publication"
                )
            else:
                publication_error = (
                    "staged GDS did not stabilize before publication: "
                    f"{source_snapshot_error}"
                )

    gds_publication_allowed = (
        final_outcome.staging_error is None
        and publication_error is None
        and local_log_path is not None
        and not cleanup_failures
        and (not skill_errors or _is_indeterminate_skill_timeout(skill_errors))
        and _has_valid_zero_error_completion(final_outcome.log)
    )

    can_publish_gds = (
        gds_publication_allowed
        and publication_error is None
        and gds_revalidated
        and final_gds_present
        and final_gds_size > 0
    )

    if final_gds_present and not can_publish_gds:
        _remove_unvalidated_gds(paths, budget, result_warnings)

    if final_outcome.staging_error is not None:
        if publication_error is not None:
            errors.append(publication_error)
        status = ExecutionStatus.ERROR
        reason = GdsExportReason.STAGING_ERROR
        timed_out = (
            final_outcome.deadline_expired
            or budget.remaining(finalizing=True) <= 0.0
        )
    elif publication_error is not None:
        errors.append(publication_error)
        status = ExecutionStatus.ERROR
        reason = GdsExportReason.PUBLICATION_ERROR
        timed_out = (
            publication_timed_out
            or budget.remaining(finalizing=True) <= 0.0
        )
    else:
        status, reason, timed_out = _classify_export(
            cleanup_failures=cleanup_failures,
            log=final_outcome.log,
            skill_errors=skill_errors,
            launch_indeterminate=launch_indeterminate,
            saw_evidence=final_outcome.saw_evidence,
            gds_present=final_gds_present,
            gds_size=final_gds_size,
            gds_published=local_gds_path is not None,
            deadline_expired=final_outcome.deadline_expired,
        )

    local_run_dir: Path | None = paths.run_dir
    should_cleanup = (
        inputs.cleanup_policy == "success" and status == ExecutionStatus.SUCCESS
    ) or inputs.cleanup_policy == "always"
    if inputs.cleanup_policy == "always" and local_log_path is None:
        should_cleanup = False
    if should_cleanup:
        if budget.remaining(finalizing=True) <= 0.0:
            result_warnings.append(
                "cleanup skipped because the export deadline expired"
            )
        else:
            try:
                shutil.rmtree(paths.run_dir)
            except FileNotFoundError:
                local_run_dir = None
            except OSError as exc:
                result_warnings.append(
                    f"local XStream cleanup failed: {exc}"
                )
            else:
                local_run_dir = None
                if budget.remaining(finalizing=True) <= 0.0:
                    result_warnings.append(
                        "local XStream cleanup completed after the export "
                        "deadline"
                    )

    return GdsExportResult(
        status=status,
        reason=reason,
        timed_out=timed_out,
        library=inputs.library,
        cell=inputs.cell,
        view=inputs.view,
        execution_time=budget.elapsed(),
        local_gds_path=local_gds_path,
        local_log_path=local_log_path,
        log_result=final_outcome.log,
        errors=tuple(errors),
        warnings=tuple(result_warnings),
        local_run_dir=local_run_dir,
    )


def _remove_unvalidated_gds(
    paths: _ExportPaths,
    budget: _Budget,
    warnings: list[str],
) -> None:
    try:
        budget.timeout(finalizing=True)
        paths.gds.unlink()
    except _BudgetExpired:
        warnings.append(
            "export time budget exhausted before unvalidated GDS cleanup"
        )
    except FileNotFoundError:
        pass
    except OSError as exc:
        warnings.append(f"failed to remove unvalidated GDS: {exc}")


def _result_errors(
    outcome: _PollOutcome,
    *,
    skill_errors: tuple[str, ...],
    cleanup_failures: tuple[str, ...],
) -> list[str]:
    errors = list(cleanup_failures)
    errors.extend(skill_errors)
    if outcome.staging_error is not None:
        errors.append(outcome.staging_error)
    if outcome.log is not None:
        errors.extend(outcome.log.terminal_failures)
        errors.extend(outcome.log.parse_errors)
        errors.extend(outcome.log.errors)
        if outcome.log.error_count not in (None, 0):
            errors.append(
                f"XStream reported {outcome.log.error_count} error(s)"
            )
    return errors


def _diagnostic_log_text(
    inputs: _ExportInputs,
    errors: list[str],
) -> str:
    lines = [
        "Virtuoso Bridge XStream export diagnostics",
        f"library: {inputs.library}",
        f"cell: {inputs.cell}",
        f"view: {inputs.view}",
    ]
    if errors:
        lines.extend(f"error: {error}" for error in errors)
    else:
        lines.append("error: no XStream log was produced for this run")
    return "\n".join(lines) + "\n"


class _EmptyPublicationFileError(OSError):
    """Raised before replacement when a copied publication file is empty."""


class _SourceSnapshotChanged(OSError):
    """Raised when a publication source changes before replacement."""


def _publication_temp_path(destination: Path) -> Path:
    return destination.parent / f".vbp-{uuid.uuid4().hex}.tmp"


def _publication_source_snapshot(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _publish_file(
    source: Path,
    destination: Path,
    *,
    validator: Callable[[], None] | None = None,
) -> None:
    temporary = _publication_temp_path(destination)
    try:
        with source.open("rb", buffering=0) as source_file:
            source_metadata_before = os.fstat(source_file.fileno())
            source_snapshot_before = _publication_source_snapshot(
                source_metadata_before
            )
            try:
                destination_metadata = destination.stat()
            except FileNotFoundError:
                destination_metadata = None
                publication_mode = stat.S_IMODE(source_metadata_before.st_mode)
            else:
                publication_mode = stat.S_IMODE(destination_metadata.st_mode)

            with temporary.open("wb") as temporary_file:
                shutil.copyfileobj(source_file, temporary_file)
                temporary_file.flush()
                source_metadata_after = os.fstat(source_file.fileno())
                temporary_size = os.fstat(temporary_file.fileno()).st_size

            source_snapshot_after = _publication_source_snapshot(
                source_metadata_after
            )
            if source_snapshot_after != source_snapshot_before:
                raise _SourceSnapshotChanged(
                    f"source changed while copying {source}"
                )
            if temporary_size != source_metadata_before.st_size:
                raise _SourceSnapshotChanged(
                    "copied publication size does not match source snapshot "
                    f"for {source}"
                )
            if temporary_size <= 0:
                raise _EmptyPublicationFileError(
                    f"refusing to publish empty file copied from {source}"
                )

            temporary.chmod(publication_mode)
            temporary_metadata = temporary.stat()
            if (
                os.name == "posix"
                and destination_metadata is not None
                and temporary_metadata.st_uid != destination_metadata.st_uid
            ):
                raise PermissionError(
                    "refusing to replace destination with a different owner"
                )

            try:
                current_source_metadata = source.stat()
            except FileNotFoundError as exc:
                raise _SourceSnapshotChanged(
                    f"source path disappeared before publication: {source}"
                ) from exc
            if (
                _publication_source_snapshot(current_source_metadata)
                != source_snapshot_after
            ):
                raise _SourceSnapshotChanged(
                    f"source path changed before publication: {source}"
                )

            if validator is not None:
                validator()
                source_metadata_after_validator = os.fstat(
                    source_file.fileno()
                )
                try:
                    current_source_metadata = source.stat()
                except FileNotFoundError as exc:
                    raise _SourceSnapshotChanged(
                        "source path disappeared during publication validation: "
                        f"{source}"
                    ) from exc
                if (
                    _publication_source_snapshot(
                        source_metadata_after_validator
                    )
                    != source_snapshot_after
                    or _publication_source_snapshot(current_source_metadata)
                    != source_snapshot_after
                ):
                    raise _SourceSnapshotChanged(
                        "source changed during publication validation: "
                        f"{source}"
                    )
            os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _staging_error_result(
    inputs: _ExportInputs,
    budget: _Budget,
    error: str,
    *,
    local_run_dir: Path | None,
    timed_out: bool,
) -> GdsExportResult:
    return GdsExportResult(
        status=ExecutionStatus.ERROR,
        reason=GdsExportReason.STAGING_ERROR,
        timed_out=(
            timed_out
            or budget.remaining(finalizing=False) <= 0.0
            or budget.remaining(finalizing=True) <= 0.0
        ),
        library=inputs.library,
        cell=inputs.cell,
        view=inputs.view,
        execution_time=budget.elapsed(),
        errors=(error,),
        local_run_dir=local_run_dir,
    )


__all__ = ["GdsExportReason", "GdsExportResult", "export_gds"]

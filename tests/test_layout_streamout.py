from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import pytest

from virtuoso_bridge import SSHClient, VirtuosoClient
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso import layout as layout_api
from virtuoso_bridge.virtuoso.layout import streamout
from virtuoso_bridge.virtuoso.layout.streamout import (
    GdsExportReason,
    GdsExportResult,
    _Budget,
    _BudgetExpired,
    _classify_export,
    _is_indeterminate_skill_timeout,
    _validate_export_inputs,
)
from virtuoso_bridge.virtuoso.layout.xstream import XStreamLogResult


Status = ExecutionStatus
Reason = GdsExportReason


@pytest.fixture(autouse=True)
def isolated_scratch_configuration(monkeypatch, tmp_path):
    from virtuoso_bridge import env
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    monkeypatch.delenv("VB_CLIENT_ID", raising=False)
    configuration = tmp_path / "offline.env"
    configuration.write_text("", encoding="utf-8")
    monkeypatch.setattr(env, "_RUNTIME_ENV_FILE", configuration)

_GDS_EXPORT_RESULT_FIELDS = (
    "status",
    "reason",
    "timed_out",
    "library",
    "cell",
    "view",
    "execution_time",
    "local_gds_path",
    "local_log_path",
    "log_result",
    "errors",
    "warnings",
    "remote_run_dir",
    "local_run_dir",
    "remote_files_retained",
)
_EXPORT_INPUT_FIELDS = (
    "library",
    "cell",
    "view",
    "output_path",
    "log_path",
    "stream_map",
    "timeout",
    "poll_interval",
    "skill_timeout",
    "finalization_reserve",
    "cleanup_policy",
)
_SKILL_FIELD_NAMES = (
    "library",
    "topCell",
    "view",
    "strmFile",
    "layerMap",
    "logFile",
    "runDir",
)

_OLD_GDS = b"old-gds"
_NEW_GDS = b"current-run-gds"
_PARTIAL_LOG = "\n".join(
    [
        "Product : Virtuoso(R) XStream Out",
        "Started at: SYNTHETIC_TIME",
        "Translating cellview demo/top/layout as STRUCTURE top.",
    ]
) + "\n"
_SUCCESS_LOG = (
    _PARTIAL_LOG
    + "INFO (XSTRM-234): Translation completed. 0 error(s) and "
    "0 warning(s) found.\n"
)
_TERMINAL_LINE = "INFO (XSTRM-273): Translation failed."
_TERMINAL_LOG = _PARTIAL_LOG + _TERMINAL_LINE + "\n"
_MALFORMED_LOG = (
    _PARTIAL_LOG
    + "INFO (XSTRM-234): Translation completed. unavailable error(s) and "
    "unavailable warning(s) found.\n"
)
_ERROR_COMPLETION_LOG = (
    _PARTIAL_LOG
    + "ERROR: current-run XStream error matching nonzero completion count\n"
    + "INFO (XSTRM-234): Translation completed. 2 error(s) and "
    "1 warning(s) found.\n"
)
_PARTIAL_WITH_ERROR_LOG = (
    _PARTIAL_LOG + "ERROR: current-run XStream diagnostic before completion\n"
)
_ZERO_COUNT_ERROR = "ERROR: current-run XStream error despite zero completion count"
_ZERO_COUNT_WITH_ERROR_LOG = (
    _PARTIAL_LOG
    + _ZERO_COUNT_ERROR
    + "\nINFO (XSTRM-234): Translation completed. 0 error(s) and "
    "0 warning(s) found.\n"
)
_LAUNCH_LOG = _PARTIAL_LOG + "INFO: launch diagnostic\n"
_FULL_LOG_SUFFIX = (
    "\n".join(f"detail {index:03d}" for index in range(220))
    + "\nINFO (XSTRM-234): Translation completed. 0 error(s) and "
    "0 warning(s) found.\n"
)
# Same size and same 200-line tail: only the full-log digest sees the rewrite.
_EARLY_TERMINAL_LOG = _PARTIAL_LOG + _TERMINAL_LINE + "\n" + _FULL_LOG_SUFFIX
_EARLY_BENIGN_LOG = (
    _PARTIAL_LOG
    + "ordinary early diagnostic".ljust(len(_TERMINAL_LINE))
    + "\n"
    + _FULL_LOG_SUFFIX
)
_STARTED_WIRE = '("xstreamRequest" "started" nil nil)'
_TIMEOUT_ERROR = "SKILL execution timeout in Virtuoso"
_SKILL_FAILURE = VirtuosoResult(
    status=ExecutionStatus.ERROR,
    errors=["explicit SKILL failure"],
)
_REJECTED = VirtuosoResult(
    status=ExecutionStatus.ERROR,
    errors=["remote launch rejected"],
)
_TOKEN = "VBXSTREAM_abcdef0123456789"
_REMOTE_ROOT = PurePosixPath("/tmp/virtuoso_bridge_remote/client/xstream")


class _FakeClock:
    def __init__(
        self,
        now: float,
        on_sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.now = now
        self.on_sleep = on_sleep
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0.0
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class _SequenceClock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)
        self.now = values[-1]

    def __call__(self) -> float:
        try:
            self.now = next(self._values)
        except StopIteration:
            pass
        return self.now


class _FakeLocalClient:
    ssh_runner = None

    def __init__(self, execute: Callable[[str, float], object]) -> None:
        self._execute = execute
        self.skill_calls: list[tuple[str, float]] = []
        self.staged: tuple[Path, Path, Path] | None = None

    def execute_skill(self, skill_code: str, timeout: float) -> object:
        assert timeout > 0.0
        self.skill_calls.append((skill_code, timeout))
        return self._execute(skill_code, timeout)

    def _forbidden(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("local export must not use transfer or X11 APIs")

    upload_file = download_file = dismiss_dialog = _forbidden


class _FakeRemoteRunner:
    _vb_remote_home = "/home/designer"

    def __init__(
        self,
        *,
        user: str | None = "remote/user",
        whoami: CommandResult | None = None,
        mkdir: CommandResult | None = None,
        log_text: str | None = None,
        gds: bytes | None = None,
        cleanup: object | None = None,
        discard: object | None = None,
    ) -> None:
        self.user = user
        self.whoami = whoami or CommandResult(0, "ignored\n", "")
        self.mkdir = mkdir or CommandResult(0, "", "")
        self.log_text = log_text
        self.gds = gds
        self.cleanup = cleanup or CommandResult(0, "", "")
        self.discard = discard or CommandResult(0, "", "")
        self.calls: list[tuple[str, float]] = []

    def run_command(self, command: str, timeout: float) -> CommandResult:
        assert timeout > 0.0
        self.calls.append((command, timeout))
        if command == "whoami":
            return self.whoami
        if "VBXSTREAM_STAGE_READY" in command:
            if self.mkdir.returncode == 0 and not self.mkdir.stdout:
                return CommandResult(
                    0,
                    "VBXSTREAM_STAGE_CREATED\nVBXSTREAM_STAGE_READY\n",
                    self.mkdir.stderr,
                )
            return self.mkdir
        if " LOG_SIZE " in command and " GDS_SIZE " in command:
            match = re.search(r"VBXSTREAM_[A-Za-z0-9]+", command)
            assert match is not None
            token = match.group(0)
            include_digests = "LOG_SHA256" in command
            lines: list[str] = []
            if self.log_text is None:
                lines.append(f"{token} LOG_MISSING")
            else:
                tail = "\n".join(self.log_text.splitlines()[-200:])
                log_bytes = self.log_text.encode("utf-8")
                lines.append(f"{token} LOG_SIZE {len(log_bytes)}")
                if include_digests:
                    lines.append(
                        f"{token} LOG_SHA256 "
                        f"{hashlib.sha256(log_bytes).hexdigest()}"
                    )
                lines.extend(
                    [f"{token} LOG_BEGIN", tail, f"{token} LOG_END"]
                )
            if self.gds is None:
                lines.append(f"{token} GDS_MISSING")
            else:
                lines.append(f"{token} GDS_SIZE {len(self.gds)}")
                if include_digests:
                    lines.append(
                        f"{token} GDS_SHA256 "
                        f"{hashlib.sha256(self.gds).hexdigest()}"
                    )
            return CommandResult(0, "\n".join(lines) + "\n", "")
        if "rm -f " in command:
            if isinstance(self.discard, BaseException):
                raise self.discard
            assert isinstance(self.discard, CommandResult)
            return self.discard
        if "rm -rf " in command:
            if isinstance(self.cleanup, BaseException):
                raise self.cleanup
            assert isinstance(self.cleanup, CommandResult)
            return self.cleanup
        raise AssertionError(f"unexpected remote command: {command}")


_DownloadHook = Callable[[_FakeRemoteRunner, str, int], object]


class _FakeRemoteClient:
    """Remote client whose downloads serve the runner's current artifacts.

    ``on_download(runner, name, count)`` may mutate the runner, return bytes to
    serve instead of the current artifact, return a response to report, or
    raise to simulate a transfer failure.
    """

    def __init__(
        self,
        runner: _FakeRemoteRunner,
        *,
        upload_result: object | None = None,
        skill_result: object | None = None,
        on_download: _DownloadHook | None = None,
    ) -> None:
        self.ssh_runner = runner
        self.upload_result = upload_result or VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
        )
        self.skill_result = (
            _started_result() if skill_result is None else skill_result
        )
        self.on_download = on_download
        self.uploads: list[tuple[Path, str, float]] = []
        self.skills: list[tuple[str, float]] = []
        self.downloads: list[tuple[str, Path, float]] = []

    @property
    def download_names(self) -> list[str]:
        return [PurePosixPath(remote).name for remote, _local, _ in self.downloads]

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        *,
        timeout: float,
    ) -> object:
        assert timeout > 0.0
        self.uploads.append((Path(local_path), remote_path, timeout))
        return self.upload_result

    def execute_skill(self, skill_code: str, timeout: float) -> object:
        assert timeout > 0.0
        self.skills.append((skill_code, timeout))
        if isinstance(self.skill_result, BaseException):
            raise self.skill_result
        return self.skill_result

    def download_file(
        self,
        remote_path: str,
        local_path: Path,
        *,
        timeout: float,
    ) -> object:
        assert timeout > 0.0
        self.downloads.append((remote_path, Path(local_path), timeout))
        name = PurePosixPath(remote_path).name
        served = None
        if self.on_download is not None:
            served = self.on_download(
                self.ssh_runner,
                name,
                self.download_names.count(name),
            )
            if isinstance(served, VirtuosoResult):
                return served
        if served is None:
            served = _current_remote_bytes(self.ssh_runner, name)
        Path(local_path).write_bytes(served)
        return VirtuosoResult(status=ExecutionStatus.SUCCESS)


class _UnreadableResponse:
    warnings = None

    @property
    def errors(self) -> object:
        raise RuntimeError("normalization broke")


def _current_remote_bytes(runner: _FakeRemoteRunner, name: str) -> bytes:
    current = runner.log_text if name == "xstream.log" else runner.gds
    if current is None:
        raise AssertionError("missing remote artifacts must not be downloaded")
    return current.encode("utf-8") if isinstance(current, str) else current


def _serve_then_replace(
    target: str,
    served: bytes | None = None,
    **state: object,
) -> _DownloadHook:
    def hook(runner: _FakeRemoteRunner, name: str, count: int) -> object:
        if name != target or count != 1:
            return None
        current = _current_remote_bytes(runner, name)
        for attribute, value in state.items():
            setattr(runner, attribute, value)
        return current if served is None else served

    return hook


def _churn(target: str) -> _DownloadHook:
    def hook(runner: _FakeRemoteRunner, name: str, count: int) -> object:
        if name != target:
            return None
        served = _current_remote_bytes(runner, name)
        if name == "xstream.log":
            runner.log_text = _SUCCESS_LOG + f"remote generation {count}\n"
        else:
            runner.gds = served + b"x"
        return served

    return hook


def _fail_download(target: str, failure: object) -> _DownloadHook:
    def hook(_runner: _FakeRemoteRunner, name: str, _count: int) -> object:
        if name != target:
            return None
        if isinstance(failure, BaseException):
            raise failure
        return failure

    return hook


def _skill_field(skill: str, field: str) -> str:
    match = re.search(
        rf'xstSetField\("{re.escape(field)}" "([^"]+)"\)',
        skill,
    )
    assert match is not None, f"missing XStream field {field}"
    return match.group(1)


def _request_artifacts(skill: str) -> tuple[Path, Path, Path]:
    return (
        Path(_skill_field(skill, "runDir")),
        Path(_skill_field(skill, "strmFile")),
        Path(_skill_field(skill, "logFile")),
    )


def _write_artifacts(
    skill: str,
    *,
    log_text: str | None = _SUCCESS_LOG,
    gds: bytes | None = _NEW_GDS,
) -> tuple[Path, Path, Path]:
    run_dir, gds_path, log_path = _request_artifacts(skill)
    assert gds_path == run_dir / "output.gds"
    assert log_path == run_dir / "xstream.log"
    if log_text is not None:
        log_path.write_text(log_text, encoding="utf-8")
    if gds is not None:
        gds_path.write_bytes(gds)
    return run_dir, gds_path, log_path


def _started_result() -> VirtuosoResult:
    return VirtuosoResult(
        status=ExecutionStatus.SUCCESS,
        output=_STARTED_WIRE,
    )


def _artifact_client(
    *,
    log_text: str | None = _SUCCESS_LOG,
    gds: bytes | None = _NEW_GDS,
    response: object | None = None,
) -> _FakeLocalClient:
    def execute(skill: str, _timeout: float) -> object:
        client.staged = _write_artifacts(skill, log_text=log_text, gds=gds)
        if isinstance(response, BaseException):
            raise response
        return _started_result() if response is None else response

    client = _FakeLocalClient(execute)
    return client


def _remote_client(
    *,
    log_text: str | None = _SUCCESS_LOG,
    gds: bytes | None = _NEW_GDS,
    response: object | None = None,
    on_download: _DownloadHook | None = None,
) -> _FakeRemoteClient:
    return _FakeRemoteClient(
        _FakeRemoteRunner(log_text=log_text, gds=gds),
        skill_result=response,
        on_download=on_download,
    )


def _transport_client(transport: str, **artifacts: Any) -> Any:
    if transport == "local":
        return _artifact_client(**artifacts)
    return _remote_client(**artifacts)


def _set_artifacts(client: Any, *, log_text: str, gds: bytes | None) -> None:
    if isinstance(client, _FakeRemoteClient):
        client.ssh_runner.log_text = log_text
        client.ssh_runner.gds = gds
        return
    assert client.staged is not None
    _run_dir, gds_path, log_path = client.staged
    log_path.write_text(log_text, encoding="utf-8")
    if gds is not None:
        gds_path.write_bytes(gds)


def _use_fake_time(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock | _SequenceClock,
) -> None:
    monkeypatch.setattr(streamout, "_MONOTONIC", clock)
    if isinstance(clock, _FakeClock):
        monkeypatch.setattr(streamout, "_SLEEP", clock.sleep)


def _run_export(
    client: object,
    output_path: Path,
    stream_map: Path,
    *,
    library: str = "demo",
    **overrides: Any,
) -> GdsExportResult:
    options: dict[str, Any] = {
        "stream_map": stream_map,
        "view": "layout",
        "log_path": None,
        "timeout": 4.0,
        "poll_interval": 0.5,
        "skill_timeout": 2.0,
        "finalization_reserve": 1.0,
        "cleanup_policy": "success",
    }
    options.update(overrides)
    return streamout.export_gds(client, library, "top", output_path, **options)


def _default_log(output_path: Path) -> Path:
    return output_path.with_name("top.xstream.log")


def _record_publications(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: Path | None = None,
) -> list[Path]:
    published: list[Path] = []
    real_publish = streamout._publish_file

    def publish(source: Path, destination: Path, **kwargs: Any) -> None:
        published.append(destination)
        if destination == fail:
            raise OSError("publication denied")
        real_publish(source, destination, **kwargs)

    monkeypatch.setattr(streamout, "_publish_file", publish)
    return published


def _write_stream_map(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# synthetic stream map\n", encoding="utf-8")
    return path


def _validation_kwargs(stream_map: Path) -> dict[str, object]:
    return {
        "stream_map": stream_map,
        "view": "layout",
        "log_path": None,
        "timeout": 30.0,
        "poll_interval": 0.25,
        "skill_timeout": 5.0,
        "finalization_reserve": 2.0,
        "cleanup_policy": "success",
    }


def _validate(tmp_path: Path, stream_map: Path, **overrides: object) -> Any:
    options: dict[str, Any] = {
        "library": "demo",
        "cell": "top",
        "output_path": tmp_path / "top.gds",
        **_validation_kwargs(stream_map),
    }
    options.update(overrides)
    return _validate_export_inputs(
        options.pop("library"),
        options.pop("cell"),
        options.pop("output_path"),
        **options,
    )


def _log_result(
    completed: bool = False,
    error_count: int | None = None,
    warning_count: int | None = None,
    *,
    errors: tuple[str, ...] = (),
    terminal_failures: tuple[str, ...] = (),
) -> XStreamLogResult:
    return XStreamLogResult(
        completed=completed,
        completion_line="Translation completed" if completed else None,
        error_count=error_count,
        warning_count=warning_count,
        translated_structures=(),
        warnings=(),
        errors=errors,
        terminal_failures=terminal_failures,
        parse_errors=(),
        current_run_text="",
    )


def _remote_paths(
    owned_root: str | PurePosixPath,
    run_dir: PurePosixPath | None = None,
) -> streamout._RemoteExportPaths:
    owned_root = PurePosixPath(owned_root)
    run_dir = owned_root / ("a" * 32) if run_dir is None else run_dir
    return streamout._RemoteExportPaths(
        owned_root=owned_root,
        run_dir=run_dir,
        gds=run_dir / "output.gds",
        log=run_dir / "xstream.log",
        stream_map=run_dir / "stream.map",
    )


def _sh(
    command: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _remote_scratch(
    tmp_path: Path,
    *,
    log: str | bytes | None = None,
    gds: bytes | None = None,
) -> tuple[Path, Path]:
    run_dir = tmp_path / "remote scratch"
    run_dir.mkdir()
    log_path = run_dir / "xstream.log"
    gds_path = run_dir / "output.gds"
    if log is not None:
        log_path.write_bytes(log if isinstance(log, bytes) else log.encode("utf-8"))
    if gds is not None:
        gds_path.write_bytes(gds)
    return log_path, gds_path


def _poll_command(
    log_path: Path,
    gds_path: Path,
    *,
    include_digests: bool = False,
) -> str:
    return streamout._remote_poll_command(
        PurePosixPath(log_path.as_posix()),
        PurePosixPath(gds_path.as_posix()),
        _TOKEN,
        include_digests=include_digests,
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock(0.0)
    _use_fake_time(monkeypatch, fake)
    return fake


@pytest.fixture
def stream_map(tmp_path: Path) -> Path:
    return _write_stream_map(tmp_path / "layers.map")


@pytest.fixture
def output_path(tmp_path: Path) -> Path:
    path = tmp_path / "top.gds"
    path.write_bytes(_OLD_GDS)
    return path


# Public surface and result model.


def test_layout_public_api_exports_streamout_names_and_reason_values() -> None:
    for name in (
        "XStreamExportRequest",
        "XStreamTranslatedStructure",
        "XStreamLogResult",
        "GdsExportReason",
        "GdsExportResult",
        "xstream_export_gds_skill",
        "parse_xstream_log",
        "export_gds",
    ):
        assert hasattr(layout_api, name)
        assert name in layout_api.__all__
    assert streamout.__all__ == ["GdsExportReason", "GdsExportResult", "export_gds"]
    assert {reason.value for reason in GdsExportReason} == {
        "completed",
        "xstream_failure",
        "xstream_errors",
        "request_cleanup_error",
        "skill_error",
        "launch_indeterminate",
        "incomplete_log",
        "missing_gds",
        "empty_gds",
        "malformed_log",
        "staging_error",
        "transport_error",
        "publication_error",
    }
    assert all(isinstance(reason, str) for reason in GdsExportReason)


def test_layout_ops_export_gds_delegates_all_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = object()
    sentinel = object()
    output_path = Path("build/top.gds")
    options: dict[str, object] = {
        "stream_map": Path("pdk/stream.map"),
        "view": "maskLayout",
        "log_path": Path("build/top.log"),
        "timeout": 120.0,
        "poll_interval": 0.25,
        "skill_timeout": 12.0,
        "finalization_reserve": 18.0,
        "cleanup_policy": "never",
        "recovery_hook": lambda: None,
    }
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_export_gds(*args: object, **kwargs: object) -> object:
        captured.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(layout_api, "export_gds", fake_export_gds)

    result = layout_api.LayoutOps(owner).export_gds(
        "worklib",
        "top",
        output_path,
        **options,  # type: ignore[arg-type]
    )

    assert result is sentinel
    assert captured == [((owner, "worklib", "top", output_path), options)]


def test_gds_export_result_is_frozen_with_tuple_diagnostics_and_ok_flag() -> None:
    errors = ["XSTRM error"]
    warnings = ["XSTRM warning"]
    result = GdsExportResult(
        status=Status.FAILURE,
        reason=Reason.XSTREAM_ERRORS,
        timed_out=False,
        library="demo",
        cell="top",
        view="layout",
        execution_time=1.25,
        local_gds_path=Path("top.gds"),
        local_log_path=Path("top.xstream.log"),
        log_result=_log_result(True, 0, 1),
        errors=errors,  # type: ignore[arg-type]
        warnings=warnings,  # type: ignore[arg-type]
        remote_run_dir="/tmp/run",
        local_run_dir=Path("run"),
        remote_files_retained=True,
    )
    errors.append("later error")
    warnings.append("later warning")

    assert tuple(field.name for field in fields(GdsExportResult)) == (
        _GDS_EXPORT_RESULT_FIELDS
    )
    assert result.errors == ("XSTRM error",)
    assert result.warnings == ("XSTRM warning",)
    with pytest.raises(FrozenInstanceError):
        result.cell = "other"  # type: ignore[misc]
    for status in ExecutionStatus:
        minimal = GdsExportResult(
            status=status,
            reason=Reason.COMPLETED,
            timed_out=False,
            library="demo",
            cell="top",
            view="layout",
            execution_time=0.1,
        )
        assert minimal.ok is (status == Status.SUCCESS)
        assert (minimal.errors, minimal.warnings) == ((), ())


# Input validation.


def test_validate_export_inputs_normalizes_paths_and_keeps_name_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    stream_map = _write_stream_map(home / "layers.map")
    monkeypatch.setenv("HOME", str(home))

    inputs = _validate_export_inputs(
        " demo",
        "\ttop",
        "~/exports/../top.gds",
        stream_map="~/layers.map",
        view="layout ",
        log_path=None,
        timeout=30,
        poll_interval=1,
        skill_timeout=5,
        finalization_reserve=2,
        cleanup_policy="success",
    )

    assert tuple(field.name for field in fields(type(inputs))) == _EXPORT_INPUT_FIELDS
    assert (inputs.library, inputs.cell, inputs.view) == (" demo", "\ttop", "layout ")
    assert inputs.output_path == (home / "top.gds").resolve()
    assert inputs.log_path == (home / "top.xstream.log").resolve()
    assert inputs.stream_map == stream_map.resolve()
    assert (
        inputs.timeout,
        inputs.poll_interval,
        inputs.skill_timeout,
        inputs.finalization_reserve,
        inputs.cleanup_policy,
    ) == (30.0, 1.0, 5.0, 2.0, "success")
    with pytest.raises(FrozenInstanceError):
        inputs.cell = "other"  # type: ignore[misc]


@pytest.mark.parametrize("cleanup_policy", ["success", "always", "never"])
def test_validate_export_inputs_normalizes_explicit_log_and_accepts_policies(
    tmp_path: Path,
    cleanup_policy: str,
) -> None:
    stream_map = _write_stream_map(tmp_path / "tech" / "layers.map")

    inputs = _validate(
        tmp_path,
        stream_map,
        output_path=tmp_path / "runs" / ".." / "top.gds",
        log_path=tmp_path / "logs" / ".." / "custom.log",
        cleanup_policy=cleanup_policy,
    )

    assert inputs.output_path == (tmp_path / "top.gds").resolve()
    assert inputs.log_path == (tmp_path / "custom.log").resolve()
    assert inputs.cleanup_policy == cleanup_policy


_INVALID_INPUTS = [
    *(
        pytest.param(field, value, id=f"{field}-{label}")
        for field in ("library", "cell", "view")
        for label, value in (
            ("empty", ""),
            ("spaces", "   "),
            ("tab", "\t"),
            ("none", None),
            ("integer", 1),
        )
    ),
    *(
        pytest.param(field, value, id=f"{field}-{label}")
        for field in (
            "timeout",
            "poll_interval",
            "skill_timeout",
            "finalization_reserve",
        )
        for label, value in (
            ("true", True),
            ("false", False),
            ("zero", 0),
            ("negative", -0.5),
            ("nan", float("nan")),
            ("positive_infinity", float("inf")),
            ("negative_infinity", -float("inf")),
            ("string", "1"),
            ("none", None),
        )
    ),
    pytest.param("finalization_reserve", 30.0, id="reserve-equals-timeout"),
    pytest.param("finalization_reserve", 31.0, id="reserve-exceeds-timeout"),
    *(
        pytest.param("cleanup_policy", value, id=f"cleanup_policy-{value!r}")
        for value in (None, "", "Success", "sometimes", True)
    ),
]


@pytest.mark.parametrize(("field", "value"), _INVALID_INPUTS)
def test_validate_export_inputs_rejects_invalid_values(
    tmp_path: Path,
    stream_map: Path,
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _validate(tmp_path, stream_map, **{field: value})


@pytest.mark.parametrize("map_kind", ["missing", "directory"])
def test_validate_export_inputs_requires_regular_stream_map(
    tmp_path: Path,
    map_kind: str,
) -> None:
    stream_map = tmp_path / "layers.map"
    if map_kind == "directory":
        stream_map.mkdir()

    with pytest.raises(FileNotFoundError):
        _validate(tmp_path, stream_map)


@pytest.mark.parametrize("alias_pair", ["output_log", "output_map", "log_map"])
@pytest.mark.parametrize("kind", ["normalized", "hardlink"])
def test_validate_export_inputs_rejects_path_aliases(
    tmp_path: Path,
    stream_map: Path,
    alias_pair: str,
    kind: str,
) -> None:
    paths = {
        "output": tmp_path / "top.gds",
        "log": tmp_path / "top.xstream.log",
        "map": stream_map,
    }
    first, second = alias_pair.split("_")
    source, alias = (second, first) if second == "map" else (first, second)
    if kind == "normalized":
        paths[alias] = tmp_path / "nested" / ".." / paths[source].name
    else:
        if not paths[source].exists():
            paths[source].write_bytes(b"synthetic gds")
        os.link(paths[source], paths[alias])

    with pytest.raises(ValueError, match="distinct"):
        _validate(
            tmp_path,
            stream_map,
            output_path=paths["output"],
            log_path=paths["log"],
        )


# Budget, timeout detection and result classification.


def test_budget_reserves_finalization_time_and_expires_by_phase() -> None:
    clock = _FakeClock(100.0)
    budget = _Budget.start(10.0, 2.0, clock=clock)

    assert (
        budget.started_at,
        budget.prefinalization_deadline,
        budget.deadline,
    ) == (100.0, 108.0, 110.0)
    assert budget.remaining(finalizing=False) == 8.0
    assert budget.remaining(finalizing=True) == 10.0
    assert budget.timeout(finalizing=False, cap=3.0) == 3.0
    assert budget.timeout(finalizing=False, cap=20.0) == 8.0
    assert budget.elapsed() == 0.0
    with pytest.raises(FrozenInstanceError):
        budget.deadline = 200.0  # type: ignore[misc]

    clock.now = 103.25
    assert budget.remaining(finalizing=False) == 4.75
    assert budget.remaining(finalizing=True) == 6.75
    assert budget.elapsed() == 3.25

    clock.now = 108.0
    assert budget.remaining(finalizing=False) == 0.0
    assert budget.remaining(finalizing=True) == 2.0
    with pytest.raises(_BudgetExpired):
        budget.timeout(finalizing=False)
    assert budget.timeout(finalizing=True) == 2.0

    clock.now = 109.999999
    timeout = budget.timeout(finalizing=True)
    assert timeout > 0.0
    assert timeout == pytest.approx(0.000001)

    clock.now = 110.0
    assert budget.remaining(finalizing=True) == 0.0
    for finalizing in (False, True):
        with pytest.raises(_BudgetExpired):
            budget.timeout(finalizing=finalizing)

    clock.now = 99.5
    assert budget.elapsed() == 0.0


@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        ((), False),
        ((_TIMEOUT_ERROR,), True),
        (("Socket timeout after 5s",), True),
        (("Socket timeout after 0.25s",), True),
        (("Socket timeout after 1e-06s",), True),
        (("Socket timeout after 2E+3s",), True),
        ((_TIMEOUT_ERROR, "Socket timeout after 12.5s"), True),
        (("prefix SKILL execution timeout in Virtuoso",), False),
        (("SKILL execution timeout in Virtuoso.",), False),
        (("Socket timeout after .5s",), False),
        (("Socket timeout after 5.s",), False),
        (("Socket timeout after -1s",), False),
        (("Socket timeout after 1.2.3s",), False),
        (("Socket timeout after 5s extra",), False),
        (("prefix Socket timeout after 5s",), False),
        (("Socket timeout after nans",), False),
        (("Socket timeout after infs",), False),
        (("Socket timeout after -infs",), False),
        (("Socket timeout after s",), False),
        (("timed out after 5s",), False),
        (("socket timeout after 5s",), False),
        (("Socket timeout after 5s", "other timeout while waiting"), False),
    ],
)
def test_indeterminate_skill_timeout_requires_every_exact_match(
    errors: tuple[str, ...],
    expected: bool,
) -> None:
    assert _is_indeterminate_skill_timeout(errors) is expected


def _classify(**observations: object) -> object:
    defaults: dict[str, object] = {
        "cleanup_failures": (),
        "log": None,
        "skill_errors": (),
        "launch_indeterminate": False,
        "saw_evidence": False,
        "gds_present": False,
        "gds_size": 0,
        "gds_published": False,
        "deadline_expired": False,
    }
    defaults.update(observations)
    return _classify_export(**defaults)  # type: ignore[arg-type]


_TERMINAL = ("XSTRM-273: Translation failed",)
_EXPLICIT = ("explicit SKILL failure",)


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        pytest.param(
            {
                "cleanup_failures": ("failed to restore logFile",),
                "log": _log_result(True, terminal_failures=_TERMINAL),
                "skill_errors": _EXPLICIT,
                "launch_indeterminate": True,
                "deadline_expired": True,
            },
            (Status.ERROR, Reason.REQUEST_CLEANUP_ERROR, True),
            id="cleanup-wins-over-all-overlapping-observations",
        ),
        pytest.param(
            {
                "log": _log_result(True, None, 0, terminal_failures=_TERMINAL),
                "skill_errors": _EXPLICIT,
                "deadline_expired": True,
            },
            (Status.FAILURE, Reason.XSTREAM_FAILURE, True),
            id="terminal-log-wins-over-non-timeout-skill-error",
        ),
        pytest.param(
            {
                "log": _log_result(True, None, 0, errors=("ERROR: diagnostic",)),
                "skill_errors": _EXPLICIT,
                "deadline_expired": True,
            },
            (Status.ERROR, Reason.MALFORMED_LOG, True),
            id="malformed-error-count-wins-over-non-timeout-skill-error",
        ),
        pytest.param(
            {"log": _log_result(True, 0, None), "skill_errors": _EXPLICIT},
            (Status.ERROR, Reason.MALFORMED_LOG, False),
            id="malformed-warning-count-wins-over-non-timeout-skill-error",
        ),
        pytest.param(
            {
                "log": _log_result(True, 2, 0, errors=("ERROR: diagnostic",)),
                "skill_errors": _EXPLICIT,
                "deadline_expired": True,
            },
            (Status.FAILURE, Reason.XSTREAM_ERRORS, True),
            id="xstream-error-count-wins-over-non-timeout-skill-error",
        ),
        pytest.param(
            {
                "log": _log_result(True, 0, 0),
                "skill_errors": ("Socket timeout after 1.5s", *_EXPLICIT),
                "deadline_expired": True,
            },
            (Status.ERROR, Reason.SKILL_ERROR, False),
            id="non-timeout-skill-error-wins-over-missing-gds",
        ),
        pytest.param(
            {"log": _log_result(True, 0, 0)},
            (Status.PARTIAL, Reason.MISSING_GDS, True),
            id="valid-completion-with-missing-gds",
        ),
        pytest.param(
            {"log": _log_result(True, 0, 1), "gds_present": True},
            (Status.PARTIAL, Reason.EMPTY_GDS, True),
            id="valid-completion-with-empty-gds",
        ),
        pytest.param(
            {"log": _log_result()},
            (Status.PARTIAL, Reason.INCOMPLETE_LOG, True),
            id="returned-launch-with-incomplete-log",
        ),
        pytest.param(
            {"launch_indeterminate": True, "saw_evidence": True},
            (Status.PARTIAL, Reason.INCOMPLETE_LOG, True),
            id="indeterminate-launch-with-evidence-is-incomplete-log",
        ),
        pytest.param(
            {
                "skill_errors": ("Socket timeout after 30s",),
                "launch_indeterminate": True,
            },
            (Status.PARTIAL, Reason.LAUNCH_INDETERMINATE, True),
            id="indeterminate-launch-without-evidence",
        ),
        pytest.param(
            {
                "log": _log_result(True, 0, 2, errors=("ERROR: retained",)),
                "gds_present": True,
                "gds_size": 1024,
                "gds_published": True,
                "deadline_expired": True,
            },
            (Status.SUCCESS, Reason.COMPLETED, False),
            id="valid-completion-with-published-nonempty-gds",
        ),
    ],
)
def test_classify_export_applies_exact_priority_matrix(
    observations: dict[str, object],
    expected: tuple[ExecutionStatus, GdsExportReason, bool],
) -> None:
    assert _classify(**observations) == expected


def test_classify_export_rejects_unclassifiable_publication_invariant() -> None:
    with pytest.raises(ValueError, match="unclassifiable"):
        _classify(
            log=_log_result(True, 0, 0),
            saw_evidence=True,
            gds_present=True,
            gds_size=1,
        )


# End-to-end behaviour shared by the local and remote transports.

_E2E_CASES = [
    pytest.param(
        _SUCCESS_LOG, None, None,
        (Status.PARTIAL, Reason.MISSING_GDS, True), 0, None,
        id="missing-gds",
    ),
    pytest.param(
        _SUCCESS_LOG, b"", None,
        (Status.PARTIAL, Reason.EMPTY_GDS, True), 0, None,
        id="empty-gds",
    ),
    pytest.param(
        _PARTIAL_LOG, None, None,
        (Status.PARTIAL, Reason.INCOMPLETE_LOG, True), 0, None,
        id="incomplete-log",
    ),
    pytest.param(
        _MALFORMED_LOG, b"invalid-gds", None,
        (Status.ERROR, Reason.MALFORMED_LOG, False), 0, None,
        id="malformed-log",
    ),
    pytest.param(
        _ERROR_COMPLETION_LOG, b"invalid-gds", None,
        (Status.FAILURE, Reason.XSTREAM_ERRORS, False), 0, "nonzero completion",
        id="xstream-errors",
    ),
    pytest.param(
        _TERMINAL_LOG, b"invalid-gds", None,
        (Status.FAILURE, Reason.XSTREAM_FAILURE, False), 0, _TERMINAL_LINE,
        id="terminal",
    ),
    pytest.param(
        _ZERO_COUNT_WITH_ERROR_LOG,
        b"diagnostic-gds",
        VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output=_STARTED_WIRE,
            warnings=["bridge warning"],
        ),
        (Status.SUCCESS, Reason.COMPLETED, False), 0, _ZERO_COUNT_ERROR,
        id="zero-count-with-error-line",
    ),
    pytest.param(
        _SUCCESS_LOG,
        b"blocked-gds",
        {
            "status": "success",
            "output": (
                '("xstreamRequest" "started" nil '
                '("failed to restore XStream field logFile"))'
            ),
        },
        (Status.ERROR, Reason.REQUEST_CLEANUP_ERROR, False), 0,
        "failed to restore XStream field logFile",
        id="request-cleanup-beats-artifacts",
    ),
    pytest.param(
        _SUCCESS_LOG, b"blocked-gds", _SKILL_FAILURE,
        (Status.ERROR, Reason.SKILL_ERROR, False), 0, "explicit SKILL failure",
        id="skill-error",
    ),
    pytest.param(
        _LAUNCH_LOG, b"unvalidated", {"status": "error", "errors": []},
        (Status.ERROR, Reason.SKILL_ERROR, False), 0,
        "SKILL request returned non-success status",
        id="skill-status-only",
    ),
    pytest.param(
        _LAUNCH_LOG,
        b"unvalidated",
        {
            "status": "success",
            "output": '("xstreamRequest" "failed" "body failed" nil)',
        },
        (Status.ERROR, Reason.SKILL_ERROR, False), 0, "body failed",
        id="skill-body-failed",
    ),
    pytest.param(
        _LAUNCH_LOG, b"unvalidated", {"status": "success", "output": "malformed wire"},
        (Status.ERROR, Reason.SKILL_ERROR, False), 0,
        "malformed XStream request response",
        id="skill-malformed-wire",
    ),
    pytest.param(
        None, None, RuntimeError("execute transport broke"),
        (Status.ERROR, Reason.SKILL_ERROR, False), 0, "execute transport broke",
        id="execute-raises",
    ),
    pytest.param(
        None, None, _UnreadableResponse(),
        (Status.ERROR, Reason.SKILL_ERROR, False), 0, "normalization broke",
        id="response-normalization-raises",
    ),
    *(
        pytest.param(
            log_text, b"diagnostic-only-gds", _SKILL_FAILURE,
            (*expected, False), 0, "explicit SKILL failure",
            id=f"skill-error-yields-to-{name}-log",
        )
        for name, log_text, expected in (
            ("terminal", _TERMINAL_LOG, (Status.FAILURE, Reason.XSTREAM_FAILURE)),
            ("malformed", _MALFORMED_LOG, (Status.ERROR, Reason.MALFORMED_LOG)),
            ("nonzero", _ERROR_COMPLETION_LOG, (Status.FAILURE, Reason.XSTREAM_ERRORS)),
        )
    ),
    pytest.param(
        None, None, {"status": "error", "errors": ["Socket timeout after 1.5s"]},
        (Status.PARTIAL, Reason.LAUNCH_INDETERMINATE, True), 0,
        "Socket timeout after 1.5s",
        id="timeout-without-progress-is-indeterminate",
    ),
    pytest.param(
        _SUCCESS_LOG,
        b"recovered-gds",
        {
            "status": ExecutionStatus.ERROR,
            "errors": [_TIMEOUT_ERROR],
            "warnings": ["bridge warning"],
        },
        (Status.SUCCESS, Reason.COMPLETED, False), 1, _TIMEOUT_ERROR,
        id="timeout-recovers-from-current-artifacts",
    ),
    pytest.param(
        _PARTIAL_LOG,
        None,
        VirtuosoResult(status=ExecutionStatus.ERROR, errors=[_TIMEOUT_ERROR]),
        (Status.PARTIAL, Reason.INCOMPLETE_LOG, True), None, None,
        id="timeout-without-hook-stays-headless",
    ),
]


@pytest.mark.parametrize("transport", ["local", "remote"])
@pytest.mark.parametrize(
    ("log_text", "gds", "response", "expected", "hook_calls", "diagnostic"),
    _E2E_CASES,
)
def test_export_gds_classifies_outcomes_identically_for_both_transports(
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    transport: str,
    log_text: str | None,
    gds: bytes | None,
    response: object,
    expected: tuple[ExecutionStatus, GdsExportReason, bool],
    hook_calls: int | None,
    diagnostic: str | None,
) -> None:
    client = _transport_client(transport, log_text=log_text, gds=gds, response=response)
    recoveries: list[int] = []
    # Without an explicit hook the default must stay headless.
    hook = {} if hook_calls is None else {"recovery_hook": lambda: recoveries.append(1)}

    result = _run_export(
        client,
        output_path,
        stream_map,
        poll_interval=0.8,
        cleanup_policy="never",
        **hook,
    )

    assert (result.status, result.reason, result.timed_out) == expected
    # Terminal outcomes return without sleeping; waiting ones sleep exactly up
    # to the pre-finalization deadline (timeout 4s minus reserve 1s), with the
    # last 0.8s poll interval truncated.
    assert sum(clock.sleeps) == pytest.approx(3.0 if result.timed_out else 0.0)
    assert len(recoveries) == (hook_calls or 0)
    if diagnostic is not None:
        assert any(diagnostic in error for error in result.errors)
    if isinstance(response, dict):
        response_warnings = response.get("warnings", [])
    else:
        response_warnings = getattr(response, "warnings", None) or []
    assert set(response_warnings) <= set(result.warnings)
    if result.status == Status.SUCCESS:
        assert result.local_gds_path == output_path
        assert output_path.read_bytes() == gds
    else:
        assert result.local_gds_path is None
        assert output_path.read_bytes() == _OLD_GDS
        if gds is not None and transport == "local":
            assert result.local_run_dir is not None
            assert not (result.local_run_dir / "output.gds").exists()
        elif gds is not None:
            commands = [command for command, _ in client.ssh_runner.calls]
            assert any("rm -f " in command for command in commands)
    if transport == "local":
        assert result.local_log_path == _default_log(output_path)
        published_log = result.local_log_path.read_text(encoding="utf-8")
        assert published_log == (log_text or published_log)
        if log_text is None:
            assert result.errors[0] in published_log


@pytest.mark.parametrize("transport", ["local", "remote"])
def test_export_gds_polls_generic_error_until_zero_count_completion(
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    transport: str,
) -> None:
    client = _transport_client(
        transport,
        log_text=_PARTIAL_WITH_ERROR_LOG,
        gds=None,
    )
    clock.on_sleep = lambda _seconds: _set_artifacts(
        client,
        log_text=_ZERO_COUNT_WITH_ERROR_LOG,
        gds=b"finished-after-one-poll",
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        poll_interval=0.25,
        cleanup_policy="never",
    )

    assert (result.status, result.reason) == (Status.SUCCESS, Reason.COMPLETED)
    assert result.local_gds_path == output_path
    assert result.log_result is not None
    assert result.errors == result.log_result.errors == (_ZERO_COUNT_ERROR,)
    assert output_path.read_bytes() == b"finished-after-one-poll"
    assert clock.sleeps == [0.25]


@pytest.mark.parametrize("transport", ["local", "remote"])
def test_export_gds_recovery_hook_runs_once_after_progress_and_only_warns(
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    transport: str,
) -> None:
    client = _transport_client(
        transport,
        log_text=_PARTIAL_LOG,
        gds=None,
        response={"status": "error", "errors": [_TIMEOUT_ERROR]},
    )

    def progress(_seconds: float) -> None:
        if len(clock.sleeps) == 1:
            log_text = _PARTIAL_LOG + "WARNING: recovery still in progress\n"
            _set_artifacts(client, log_text=log_text, gds=None)
        else:
            _set_artifacts(client, log_text=_SUCCESS_LOG, gds=b"recovered")

    hook_calls: list[str] = []

    def recovery_hook() -> None:
        hook_calls.append("called")
        raise RuntimeError("X11 recovery unavailable")

    clock.on_sleep = progress

    result = _run_export(
        client,
        output_path,
        stream_map,
        recovery_hook=recovery_hook,
    )

    assert result.status == Status.SUCCESS
    assert output_path.read_bytes() == b"recovered"
    assert hook_calls == ["called"]
    assert result.warnings == ("recovery hook failed: X11 recovery unavailable",)
    assert clock.sleeps == [0.5, 0.5]


@pytest.mark.parametrize("transport", ["local", "remote"])
@pytest.mark.parametrize("failing", ["log", "gds"])
def test_export_gds_publication_failure_never_replaces_old_gds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    transport: str,
    failing: str,
) -> None:
    log_path = tmp_path / "top.log"
    log_path.write_text("old-log\n", encoding="utf-8")
    client = _transport_client(transport)
    published = _record_publications(
        monkeypatch,
        fail=(log_path if failing == "log" else output_path).resolve(),
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert (result.status, result.reason, result.timed_out) == (
        Status.ERROR,
        Reason.PUBLICATION_ERROR,
        False,
    )
    assert result.local_gds_path is None
    assert output_path.read_bytes() == _OLD_GDS
    assert any("publication denied" in error for error in result.errors)
    if failing == "log":
        assert published == [log_path.resolve()]
        assert result.local_log_path is None
        assert log_path.read_text(encoding="utf-8") == "old-log\n"
    else:
        assert published == [log_path.resolve(), output_path.resolve()]
        assert result.local_log_path == log_path
        assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    if transport == "remote":
        assert result.remote_files_retained is True
        assert client.download_names == (
            ["xstream.log"] if failing == "log" else ["xstream.log", "output.gds"]
        )


# Local transport.


@pytest.mark.parametrize(
    ("timeout", "skill_timeout", "expected_skill_timeout"),
    [(4.0, 2.0, 2.0), (2.0, 5.0, 1.0)],
    ids=["capped-by-skill-timeout", "capped-by-prefinal-budget"],
)
def test_export_gds_local_success_stages_sibling_run_and_publishes_log_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    timeout: float,
    skill_timeout: float,
    expected_skill_timeout: float,
) -> None:
    output_path = tmp_path / "published" / "top.gds"
    log_path = tmp_path / "logs" / "top.log"
    stream_map = _write_stream_map(tmp_path / "maps" / "layers.map")
    client = _artifact_client()
    published = _record_publications(monkeypatch)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        timeout=timeout,
        skill_timeout=skill_timeout,
    )

    assert (result.status, result.reason, result.timed_out) == (
        Status.SUCCESS,
        Reason.COMPLETED,
        False,
    )
    assert (result.library, result.cell, result.view) == ("demo", "top", "layout")
    assert result.execution_time == 0.0
    assert result.local_gds_path == output_path.resolve()
    assert result.local_log_path == log_path.resolve()
    assert (result.local_run_dir, result.remote_run_dir) == (None, None)
    assert result.remote_files_retained is None
    assert (result.errors, result.warnings) == ((), ())
    assert output_path.read_bytes() == _NEW_GDS
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert published == [log_path.resolve(), output_path.resolve()]
    [(skill, used_timeout)] = client.skill_calls
    assert used_timeout == expected_skill_timeout
    run_dir, _staged_gds, _staged_log = _request_artifacts(skill)
    assert {field: _skill_field(skill, field) for field in _SKILL_FIELD_NAMES} == {
        "library": "demo",
        "topCell": "top",
        "view": "layout",
        "strmFile": str(run_dir / "output.gds"),
        "layerMap": str(stream_map.resolve()),
        "logFile": str(run_dir / "xstream.log"),
        "runDir": str(run_dir),
    }
    assert run_dir.parent == output_path.parent.resolve()
    assert not run_dir.exists()
    assert clock.sleeps == []


def test_export_gds_localhost_tunnel_shape_uses_local_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
) -> None:
    tunnel = SSHClient(remote_host="localhost", port=65432)
    client = VirtuosoClient.from_tunnel(tunnel)
    assert client.is_remote is True
    assert client.ssh_runner is None
    calls: list[tuple[str, float]] = []

    def execute(skill: str, timeout: float) -> VirtuosoResult:
        calls.append((skill, timeout))
        _write_artifacts(skill)
        return _started_result()

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("local tunnel export must not use transfer or X11 APIs")

    monkeypatch.setattr(client, "execute_skill", execute)
    for name in ("upload_file", "download_file", "dismiss_dialog"):
        monkeypatch.setattr(client, name, forbidden)

    result = _run_export(client, output_path, stream_map)

    assert result.status == Status.SUCCESS
    assert output_path.read_bytes() == _NEW_GDS
    assert len(calls) == 1


def test_export_gds_structures_ssh_runner_getter_failure(
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
) -> None:
    class BrokenClient:
        @property
        def ssh_runner(self) -> object:
            raise RuntimeError("runner discovery failed")

    result = _run_export(BrokenClient(), output_path, stream_map)

    assert (result.status, result.reason, result.timed_out) == (
        Status.ERROR,
        Reason.TRANSPORT_ERROR,
        False,
    )
    assert (result.local_gds_path, result.local_log_path) == (None, None)
    assert result.local_run_dir is None
    assert any("runner discovery failed" in error for error in result.errors)


def test_export_gds_uses_fresh_run_dir_per_export_and_never_reports_stale_output(
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
) -> None:
    results = [
        _run_export(
            _artifact_client(log_text=None, gds=None, response=_SKILL_FAILURE),
            output_path,
            stream_map,
            cleanup_policy="never",
        )
        for _ in range(2)
    ]

    run_dirs = [result.local_run_dir for result in results]
    assert [result.reason for result in results] == [Reason.SKILL_ERROR] * 2
    assert [result.local_gds_path for result in results] == [None, None]
    assert None not in run_dirs
    assert run_dirs[0] != run_dirs[1]
    assert {run_dir.parent for run_dir in run_dirs} == {output_path.parent}
    assert output_path.read_bytes() == _OLD_GDS


@pytest.mark.parametrize(
    ("fault", "timed_out", "launched", "log_published", "diagnostic"),
    [
        ("output-parent-is-file", False, False, False, "local XStream staging"),
        ("budget-expired-before-staging", True, False, False, "budget"),
        ("budget-expired-after-staging", True, False, True, "budget exhausted"),
        ("staged-log-is-directory", False, True, True, "xstream.log"),
        ("sleep-overflows", False, True, True, "timestamp out of range"),
    ],
)
def test_export_gds_local_staging_failures_are_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    fault: str,
    timed_out: bool,
    launched: bool,
    log_published: bool,
    diagnostic: str,
) -> None:
    output_path = tmp_path / "top.gds"
    client = _artifact_client()
    options: dict[str, Any] = {"cleanup_policy": "always"}
    if fault == "output-parent-is-file":
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("blocker\n", encoding="utf-8")
        output_path = blocker / "top.gds"
    elif fault == "budget-expired-before-staging":
        _use_fake_time(monkeypatch, _SequenceClock(0.0, 4.0))
    elif fault == "budget-expired-after-staging":
        real_renderer = streamout.xstream_export_gds_skill

        def consume_prefinal_budget(request: object) -> str:
            skill = real_renderer(request)  # type: ignore[arg-type]
            clock.now = 3.0
            return skill

        monkeypatch.setattr(
            streamout,
            "xstream_export_gds_skill",
            consume_prefinal_budget,
        )
    elif fault == "staged-log-is-directory":
        def execute(skill: str, _timeout: float) -> VirtuosoResult:
            _run_dir, _gds_path, log_path = _request_artifacts(skill)
            log_path.mkdir()
            return _started_result()

        client = _FakeLocalClient(execute)
    else:
        client = _artifact_client(log_text=None, gds=None)

        def overflow(seconds: float) -> None:
            clock.sleeps.append(seconds)
            raise OverflowError("timestamp out of range")

        monkeypatch.setattr(streamout, "_SLEEP", overflow)
        options.update(timeout=1e308, poll_interval=1e308, skill_timeout=1.0)

    result = _run_export(client, output_path, stream_map, **options)

    assert (result.status, result.reason, result.timed_out) == (
        Status.ERROR,
        Reason.STAGING_ERROR,
        timed_out,
    )
    assert result.local_gds_path is None
    assert len(client.skill_calls) == int(launched)
    assert any(diagnostic in error for error in result.errors)
    if log_published:
        assert result.local_log_path == _default_log(output_path)
        assert diagnostic in result.local_log_path.read_text(encoding="utf-8")
        assert result.local_run_dir is None
    else:
        assert result.local_log_path is None
    if fault == "sleep-overflows":
        assert clock.sleeps == [pytest.approx(1e308)]


@pytest.mark.parametrize(
    ("deadline_hit", "expected", "published_log"),
    [
        ("skill-returns-at-prefinal", (Status.SUCCESS, Reason.COMPLETED), _SUCCESS_LOG),
        ("skill-returns-at-total", (Status.ERROR, Reason.PUBLICATION_ERROR), None),
        ("log-publication", (Status.ERROR, Reason.PUBLICATION_ERROR), _TERMINAL_LOG),
    ],
)
def test_export_gds_finalization_reserve_bounds_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    deadline_hit: str,
    expected: tuple[ExecutionStatus, GdsExportReason],
    published_log: str | None,
) -> None:
    log_path = tmp_path / "top.log"
    returns_at = {"skill-returns-at-prefinal": 3.0, "skill-returns-at-total": 4.0}

    def execute(skill: str, _timeout: float) -> object:
        if deadline_hit == "log-publication":
            _write_artifacts(skill, log_text=_TERMINAL_LOG, gds=None)
            return _started_result()
        _write_artifacts(skill)
        clock.now = returns_at[deadline_hit]
        return {"status": "error", "errors": [_TIMEOUT_ERROR]}

    real_publish = streamout._publish_file

    def publish(source: Path, destination: Path, **kwargs: Any) -> None:
        if deadline_hit == "skill-returns-at-total":
            raise AssertionError("publication after total deadline")
        real_publish(source, destination, **kwargs)
        if deadline_hit == "log-publication" and destination == log_path:
            clock.now = 4.0

    def forbidden_cleanup(_path: Path) -> None:
        raise AssertionError("cleanup after total deadline")

    monkeypatch.setattr(streamout, "_publish_file", publish)
    monkeypatch.setattr(streamout.shutil, "rmtree", forbidden_cleanup)
    # Failures use "always" so any cleanup attempted past the deadline is caught.
    policy = "never" if deadline_hit == "skill-returns-at-prefinal" else "always"

    result = _run_export(
        _FakeLocalClient(execute),
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy=policy,
    )

    succeeded = expected[0] == Status.SUCCESS
    assert (result.status, result.reason) == expected
    assert result.timed_out is not succeeded
    assert clock.sleeps == []
    assert result.local_gds_path == (output_path if succeeded else None)
    assert output_path.read_bytes() == (_NEW_GDS if succeeded else _OLD_GDS)
    assert result.local_run_dir is not None
    if published_log is None:
        assert result.local_log_path is None
    else:
        assert result.local_log_path == log_path
        assert log_path.read_text(encoding="utf-8") == published_log


@pytest.mark.parametrize("oversleeps", [False, True])
def test_export_gds_observes_completion_only_before_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    oversleeps: bool,
) -> None:
    client = _artifact_client(log_text=_PARTIAL_LOG, gds=None)

    def complete(_seconds: float) -> None:
        if oversleeps:
            clock.now = 5.0
        elif clock.now != pytest.approx(1.5):
            return
        _set_artifacts(client, log_text=_SUCCESS_LOG, gds=b"late-gds")

    clock.on_sleep = complete
    real_observe = streamout._observe_local_artifacts
    observations: list[Path] = []

    def record_observation(paths: Any) -> Any:
        observations.append(paths.run_dir)
        return real_observe(paths)

    monkeypatch.setattr(streamout, "_observe_local_artifacts", record_observation)

    result = _run_export(
        client,
        output_path,
        stream_map,
        timeout=2.0,
        finalization_reserve=0.5,
        poll_interval=1.0,
        cleanup_policy="never",
    )

    if not oversleeps:
        assert (result.status, result.reason) == (Status.SUCCESS, Reason.COMPLETED)
        assert output_path.read_bytes() == b"late-gds"
        assert clock.sleeps == [1.0, 0.5]
        return
    assert (result.status, result.reason, result.timed_out) == (
        Status.ERROR,
        Reason.PUBLICATION_ERROR,
        True,
    )
    assert result.log_result is not None
    assert result.log_result.completed is False
    assert (result.local_gds_path, result.local_log_path) == (None, None)
    assert result.local_run_dir is not None
    assert (result.local_run_dir / "output.gds").read_bytes() == b"late-gds"
    assert len(observations) == 1
    assert clock.sleeps == [1.0]


def test_export_gds_structures_surrogate_diagnostic_encoding_error(
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
) -> None:
    client = _artifact_client(log_text=None, gds=None, response=_SKILL_FAILURE)

    result = _run_export(client, output_path, stream_map, library="demo\udcff")

    assert (result.status, result.reason) == (Status.ERROR, Reason.PUBLICATION_ERROR)
    assert result.local_log_path is None
    assert any("surrogate" in error.lower() for error in result.errors)


_COMPLETED_STAGING = (_SUCCESS_LOG, b"must-not-publish", None)


@pytest.mark.parametrize(
    ("point", "change", "initial", "expected", "published_log"),
    [
        pytest.param(
            "before-finalize", "terminal", _COMPLETED_STAGING,
            (Status.FAILURE, Reason.XSTREAM_FAILURE, False), _TERMINAL_LOG,
            id="final-snapshot-recheck",
        ),
        pytest.param(
            "before-finalize", "terminal", (_PARTIAL_LOG, None, _SKILL_FAILURE),
            (Status.FAILURE, Reason.XSTREAM_FAILURE, False), _TERMINAL_LOG,
            id="skill-error-log-refresh",
        ),
        pytest.param(
            "after-log-publish", "terminal", _COMPLETED_STAGING,
            (Status.FAILURE, Reason.XSTREAM_FAILURE, False), _TERMINAL_LOG,
            id="log-recheck-after-log-publication",
        ),
        pytest.param(
            "after-log-publish", "empty-gds", _COMPLETED_STAGING,
            (Status.PARTIAL, Reason.EMPTY_GDS, True), _SUCCESS_LOG,
            id="gds-revalidation-after-log-publication",
        ),
        pytest.param(
            "gds-revalidation", "terminal", _COMPLETED_STAGING,
            (Status.FAILURE, Reason.XSTREAM_FAILURE, False), _TERMINAL_LOG,
            id="log-recheck-after-gds-revalidation",
        ),
        pytest.param(
            "gds-validator", "terminal", _COMPLETED_STAGING,
            (Status.FAILURE, Reason.XSTREAM_FAILURE, False), _TERMINAL_LOG,
            id="log-validation-inside-gds-publication",
        ),
        pytest.param(
            "gds-validator", "keeps-changing", _COMPLETED_STAGING,
            (Status.ERROR, Reason.PUBLICATION_ERROR, False), None,
            id="bounded-pre-replace-retries",
        ),
        pytest.param(
            "diagnostic", "success-log", (None, None, _SKILL_FAILURE),
            (Status.ERROR, Reason.SKILL_ERROR, False), _SUCCESS_LOG,
            id="late-xstream-log-beats-synthetic-diagnostic",
        ),
    ],
)
def test_export_gds_rechecks_live_staging_until_gds_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    point: str,
    change: str,
    initial: tuple[str | None, bytes | None, object],
    expected: tuple[ExecutionStatus, GdsExportReason, bool],
    published_log: str | None,
) -> None:
    log_path = tmp_path / "top.log"
    initial_log, initial_gds, response = initial
    client = _artifact_client(log_text=initial_log, gds=initial_gds, response=response)
    changes: list[Path] = []

    def change_staging(run_dir: Path) -> None:
        changes.append(run_dir)
        if change == "keeps-changing":
            late_log = _SUCCESS_LOG + f"INFO: late update {len(changes)}\n"
            (run_dir / "xstream.log").write_text(late_log, encoding="utf-8")
        elif len(changes) > 1:
            return
        elif change == "empty-gds":
            (run_dir / "output.gds").write_bytes(b"")
        else:
            replacement = _TERMINAL_LOG if change == "terminal" else _SUCCESS_LOG
            (run_dir / "xstream.log").write_text(replacement, encoding="utf-8")

    real_publish = streamout._publish_file
    real_file_size = streamout._local_file_size
    log_published: list[bool] = []

    def publish(source: Path, destination: Path, **kwargs: Any) -> None:
        validator = kwargs.get("validator")
        if point == "gds-validator" and validator is not None:
            def change_then_validate() -> None:
                [temporary] = destination.parent.glob(".vbp-*.tmp")
                assert temporary.read_bytes() == source.read_bytes()
                change_staging(source.parent)
                validator()

            kwargs["validator"] = change_then_validate
        real_publish(source, destination, **kwargs)
        if destination == log_path.resolve():
            log_published.append(True)
            if point == "after-log-publish":
                change_staging(source.parent)

    def file_size(path: Path) -> tuple[bool, int]:
        if log_published and path.name == "output.gds":
            change_staging(path.parent)
        return real_file_size(path)

    monkeypatch.setattr(streamout, "_publish_file", publish)
    if point == "gds-revalidation":
        monkeypatch.setattr(streamout, "_local_file_size", file_size)
    elif point == "before-finalize":
        real_finalize = streamout._finalize_local_export

        def finalize(inputs: Any, paths: Any, *args: Any, **kwargs: Any) -> Any:
            change_staging(paths.run_dir)
            return real_finalize(inputs, paths, *args, **kwargs)

        monkeypatch.setattr(streamout, "_finalize_local_export", finalize)
    elif point == "diagnostic":
        real_diagnostic = streamout._diagnostic_log_text

        def diagnostic(inputs: Any, errors: list[str]) -> str:
            assert client.staged is not None
            change_staging(client.staged[0])
            return real_diagnostic(inputs, errors)

        monkeypatch.setattr(streamout, "_diagnostic_log_text", diagnostic)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert (result.status, result.reason, result.timed_out) == expected
    assert result.local_gds_path is None
    assert output_path.read_bytes() == _OLD_GDS
    assert not tuple(tmp_path.glob(".vbp-*.tmp"))
    assert changes
    if published_log is not None:
        assert result.local_log_path == log_path.resolve()
        assert log_path.read_text(encoding="utf-8") == published_log
    assert result.log_result is not None
    if change == "terminal":
        assert set(result.log_result.terminal_failures) <= set(result.errors)
        assert result.log_result.terminal_failures
    if response is _SKILL_FAILURE:
        assert "explicit SKILL failure" in result.errors
    if point == "gds-validator":
        assert len(changes) == (
            streamout._MAX_FINAL_LOG_REFRESHES if change == "keeps-changing" else 1
        )
    if change == "keeps-changing":
        assert any("did not stabilize" in error for error in result.errors)
    if change == "success-log":
        assert result.log_result.completed is True
    if change == "empty-gds":
        assert result.local_run_dir is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX open-file replacement")
@pytest.mark.parametrize("rewrite", ["in-place", "path-replaced"])
def test_export_gds_retries_gds_copy_when_source_changes(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    rewrite: str,
) -> None:
    original_gds = b"A" * 16
    replacement_gds = (b"B" if rewrite == "in-place" else b"C") * 16
    client = _artifact_client(gds=original_gds)
    real_copyfileobj = streamout.shutil.copyfileobj
    copied_attempts: list[bytes] = []

    def copy_with_rewrite(
        source_file: Any,
        destination_file: Any,
        length: int = 0,
    ) -> None:
        source_path = Path(source_file.name)
        if source_path.name != "output.gds":
            real_copyfileobj(source_file, destination_file, length)
            return
        first_attempt = not copied_attempts
        if not first_attempt:
            assert output_path.read_bytes() == _OLD_GDS
        source_metadata = os.fstat(source_file.fileno())
        if first_attempt and rewrite == "in-place":
            destination_file.write(source_file.read(len(original_gds) // 2))
            source_path.write_bytes(replacement_gds)
            os.utime(
                source_path,
                ns=(
                    source_metadata.st_atime_ns,
                    source_metadata.st_mtime_ns + 1_000_000_000,
                ),
            )
        real_copyfileobj(source_file, destination_file, length)
        destination_file.flush()
        copied_attempts.append(Path(destination_file.name).read_bytes())
        if first_attempt and rewrite == "path-replaced":
            replacement_path = source_path.with_name("replacement.gds")
            replacement_path.write_bytes(replacement_gds)
            os.replace(replacement_path, source_path)
            assert not os.path.samestat(source_metadata, source_path.stat())

    monkeypatch.setattr(streamout.shutil, "copyfileobj", copy_with_rewrite)

    result = _run_export(client, output_path, stream_map, cleanup_policy="never")

    assert (result.status, result.reason) == (Status.SUCCESS, Reason.COMPLETED)
    assert result.local_gds_path == output_path
    assert result.errors == ()
    torn = b"A" * 8 + b"B" * 8
    assert copied_attempts == [
        torn if rewrite == "in-place" else original_gds,
        replacement_gds,
    ]
    assert output_path.read_bytes() == replacement_gds
    assert result.local_run_dir is not None
    assert (result.local_run_dir / "output.gds").read_bytes() == replacement_gds
    assert not tuple(output_path.parent.rglob(".vbp-*.tmp"))


def test_export_gds_uses_read_snapshot_size_for_parse_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
) -> None:
    log_path = tmp_path / "top.log"
    client = _artifact_client(log_text="", gds=b"snapshot-gds")
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    completed_before_read: list[Path] = []

    def materialize_completion(path: Path) -> None:
        if path.name == "xstream.log" and not completed_before_read:
            completed_before_read.append(path)
            path.write_bytes(_SUCCESS_LOG.encode("utf-8"))

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        materialize_completion(path)
        return real_read_text(path, *args, **kwargs)

    def read_bytes(path: Path) -> bytes:
        materialize_completion(path)
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    result = _run_export(client, output_path, stream_map, log_path=log_path)

    assert (result.status, result.reason) == (Status.SUCCESS, Reason.COMPLETED)
    assert result.errors == ()
    assert result.local_log_path == log_path.resolve()
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert output_path.read_bytes() == b"snapshot-gds"


@pytest.mark.parametrize(
    ("cleanup_policy", "scenario", "run_dir_retained"),
    [
        ("success", "success", False),
        ("success", "failure", True),
        ("always", "success", False),
        ("always", "failure", False),
        ("never", "success", True),
        ("never", "failure", True),
        ("always", "diagnostic", False),
        ("always", "unpublishable", True),
    ],
)
def test_export_gds_local_cleanup_policy_controls_run_retention(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    cleanup_policy: str,
    scenario: str,
    run_dir_retained: bool,
) -> None:
    client, expected = {
        "success": (_artifact_client(), (Status.SUCCESS, Reason.COMPLETED)),
        "failure": (
            _artifact_client(log_text=_TERMINAL_LOG, gds=None),
            (Status.FAILURE, Reason.XSTREAM_FAILURE),
        ),
        "diagnostic": (
            _artifact_client(log_text=None, gds=None, response=_SKILL_FAILURE),
            (Status.ERROR, Reason.SKILL_ERROR),
        ),
        "unpublishable": (
            _artifact_client(log_text=_TERMINAL_LOG, gds=None),
            (Status.ERROR, Reason.PUBLICATION_ERROR),
        ),
    }[scenario]
    if scenario == "unpublishable":
        _record_publications(monkeypatch, fail=_default_log(output_path))
    real_rmtree = streamout.shutil.rmtree
    removed: list[Path] = []

    def rmtree(path: Path) -> None:
        removed.append(Path(path))
        real_rmtree(path)

    monkeypatch.setattr(streamout.shutil, "rmtree", rmtree)

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy=cleanup_policy,
    )

    assert (result.status, result.reason) == expected
    assert (result.local_run_dir is not None) is run_dir_retained
    assert bool(removed) is not run_dir_retained
    if result.local_run_dir is not None:
        assert result.local_run_dir.is_dir()
    if scenario == "unpublishable":
        assert result.local_log_path is None
    else:
        assert result.local_log_path == _default_log(output_path)
    if scenario == "diagnostic":
        assert "explicit SKILL failure" in result.local_log_path.read_text(
            encoding="utf-8"
        )


_STAGING_RETAINED = (
    "GDS publication completed after the export deadline; local staging retained"
)
_CLEANUP_SKIPPED = "cleanup skipped because the export deadline expired"
_LOCAL_CLEANUP_FAILED = ("local XStream cleanup failed", "cleanup denied")


@pytest.mark.parametrize(
    ("cleanup_policy", "fault", "run_dir_retained", "warnings"),
    [
        ("success", "rmtree-fails", True, _LOCAL_CLEANUP_FAILED),
        ("always", "rmtree-fails", True, _LOCAL_CLEANUP_FAILED),
        (
            "success",
            "rmtree-crosses-deadline",
            False,
            ("local XStream cleanup completed after the export deadline",),
        ),
        ("success", "rmtree-fails-after-deadline", True, ("late cleanup failure",)),
        (
            "success",
            "gds-commit-crosses-deadline",
            True,
            (_STAGING_RETAINED, _CLEANUP_SKIPPED),
        ),
        ("never", "gds-commit-crosses-deadline", True, (_STAGING_RETAINED,)),
        ("always", "deadline-after-failure", True, (_CLEANUP_SKIPPED,)),
        ("never", "unvalidated-gds-unlink-fails", True, ("staged GDS cleanup denied",)),
    ],
)
def test_export_gds_local_cleanup_problems_only_warn(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    cleanup_policy: str,
    fault: str,
    run_dir_retained: bool,
    warnings: tuple[str, ...],
) -> None:
    failure = fault in {"deadline-after-failure", "unvalidated-gds-unlink-fails"}
    real_rmtree = streamout.shutil.rmtree
    real_unlink = Path.unlink

    def rmtree(path: Path) -> None:
        if fault in {"gds-commit-crosses-deadline", "deadline-after-failure"}:
            raise AssertionError("cleanup called after total deadline")
        if fault == "rmtree-fails":
            raise OSError("cleanup denied")
        if fault == "rmtree-fails-after-deadline":
            clock.now = 4.0
            raise OSError("late cleanup failure")
        real_rmtree(path)
        clock.now = 4.0

    monkeypatch.setattr(streamout.shutil, "rmtree", rmtree)
    if fault == "gds-commit-crosses-deadline":
        real_publish = streamout._publish_file

        def publish(source: Path, destination: Path, **kwargs: Any) -> None:
            real_publish(source, destination, **kwargs)
            if destination == output_path:
                clock.now = 4.0

        monkeypatch.setattr(streamout, "_publish_file", publish)
    elif fault == "unvalidated-gds-unlink-fails":
        def fail_staged_gds_unlink(path: Path, missing_ok: bool = False) -> None:
            if path.name == "output.gds":
                raise OSError("staged GDS cleanup denied")
            real_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_staged_gds_unlink)
    elif failure:
        real_classify = streamout._classify_export

        def classify_then_expire(**observations: object) -> object:
            classified = real_classify(**observations)  # type: ignore[arg-type]
            clock.now = 4.0
            return classified

        monkeypatch.setattr(streamout, "_classify_export", classify_then_expire)

    unvalidated = b"unvalidated" if fault == "unvalidated-gds-unlink-fails" else None
    result = _run_export(
        _artifact_client(log_text=_TERMINAL_LOG, gds=unvalidated)
        if failure
        else _artifact_client(),
        output_path,
        stream_map,
        cleanup_policy=cleanup_policy,
    )

    assert (result.status, result.reason, result.timed_out) == (
        (Status.FAILURE, Reason.XSTREAM_FAILURE, False)
        if failure
        else (Status.SUCCESS, Reason.COMPLETED, False)
    )
    assert result.errors == ((_TERMINAL_LINE,) if failure else ())
    assert result.local_gds_path == (None if failure else output_path)
    assert output_path.read_bytes() == (_OLD_GDS if failure else _NEW_GDS)
    assert result.local_log_path == _default_log(output_path)
    assert (result.local_run_dir is not None) is run_dir_retained
    if result.local_run_dir is not None:
        assert result.local_run_dir.is_dir()
    for warning in warnings:
        assert any(warning in actual for actual in result.warnings)
    if unvalidated is not None:
        staged_gds = result.local_run_dir / "output.gds"  # type: ignore[operator]
        assert staged_gds.read_bytes() == unvalidated


# Atomic file publication.


def test_publish_file_uses_temporary_sibling_and_cleans_it_on_replace_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.gds"
    destination = tmp_path / "published" / ("d" * 200 + ".gds")
    source.write_bytes(b"new-gds")
    destination.parent.mkdir()
    destination.write_bytes(_OLD_GDS)
    replace_calls: list[tuple[Path, Path]] = []

    def fail_replace(source_path: str | Path, destination_path: str | Path) -> None:
        replace_calls.append((Path(source_path), Path(destination_path)))
        raise OSError("replace denied")

    monkeypatch.setattr(streamout.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace denied"):
        streamout._publish_file(source, destination)

    [(temporary, used_destination)] = replace_calls
    assert used_destination == destination
    assert temporary.parent == destination.parent
    assert temporary.name.startswith(".vbp-")
    assert len(temporary.name) <= 48
    assert destination.name not in temporary.name
    assert not temporary.exists()
    assert destination.read_bytes() == _OLD_GDS


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode semantics")
@pytest.mark.parametrize("suffix", [".log", ".gds"])
@pytest.mark.parametrize(
    ("source_mode", "existing_mode", "expected_mode"),
    [(0o664, 0o600, 0o600), (0o640, None, 0o640)],
    ids=["preserves-existing-mode", "first-publication-inherits-source"],
)
def test_publish_file_mode_follows_existing_destination_or_source(
    tmp_path: Path,
    suffix: str,
    source_mode: int,
    existing_mode: int | None,
    expected_mode: int,
) -> None:
    source = tmp_path / f"source{suffix}"
    destination = tmp_path / f"published{suffix}"
    source.write_bytes(b"new-data")
    source.chmod(source_mode)
    if existing_mode is not None:
        destination.write_bytes(b"old-data")
        destination.chmod(existing_mode)

    streamout._publish_file(source, destination)

    metadata = destination.stat()
    assert destination.read_bytes() == b"new-data"
    assert stat.S_IMODE(metadata.st_mode) == expected_mode
    assert metadata.st_uid == os.getuid()


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner semantics")
@pytest.mark.parametrize("fault", ["empty-source", "owner-change", "source-changed"])
def test_publish_file_refuses_unsafe_replacement_and_removes_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    source = tmp_path / "source.gds"
    destination = tmp_path / "published.gds"
    source.write_bytes(b"" if fault == "empty-source" else b"copied-snapshot")
    destination.write_bytes(_OLD_GDS)
    kwargs: dict[str, Any] = {}
    expected: tuple[type[BaseException], str | None] = (OSError, "empty")
    if fault == "owner-change":
        real_stat = Path.stat

        def report_different_temp_owner(path: Path, *args: Any, **kw: Any) -> Any:
            metadata = real_stat(path, *args, **kw)
            if path.parent == tmp_path and path not in (source, destination):
                values = list(metadata)
                values[4] = metadata.st_uid + 1
                return os.stat_result(values)
            return metadata

        monkeypatch.setattr(Path, "stat", report_different_temp_owner)
        expected = (OSError, "owner")
    elif fault == "source-changed":
        kwargs["validator"] = lambda: source.write_bytes(b"newer-source")
        expected = (streamout._SourceSnapshotChanged, None)

    with pytest.raises(expected[0], match=expected[1]):
        streamout._publish_file(source, destination, **kwargs)

    assert destination.read_bytes() == _OLD_GDS
    assert not tuple(tmp_path.glob(".vbp-*.tmp"))


# Remote transport: shell commands executed by the remote host.


def test_remote_sentinel_reports_missing_artifacts_with_exit_zero(
    tmp_path: Path,
) -> None:
    log_path, gds_path = _remote_scratch(tmp_path)
    command = _poll_command(log_path, gds_path)

    completed = _sh(command)

    assert completed.returncode == 0
    observation = streamout._parse_remote_poll_output(completed.stdout, _TOKEN)
    assert observation == streamout._ArtifactObservation()
    assert shlex.quote(log_path.as_posix()) in command
    assert shlex.quote(gds_path.as_posix()) in command
    for digest_tool in ("sha256sum", "shasum", "openssl"):
        assert digest_tool not in command


def test_remote_sentinel_keeps_log_text_inside_random_frame(tmp_path: Path) -> None:
    lines = [f"line {index}" for index in range(205)]
    lines[-2] = f"{_TOKEN} GDS_SIZE 999"
    log_path, gds_path = _remote_scratch(
        tmp_path,
        log="\n".join(lines),
        gds=b"fresh-gds",
    )

    completed = _sh(_poll_command(log_path, gds_path))

    assert completed.returncode == 0
    observation = streamout._parse_remote_poll_output(completed.stdout, _TOKEN)
    assert (observation.log_present, observation.log_size) == (
        True,
        log_path.stat().st_size,
    )
    assert observation.log_text.splitlines()[0] == "line 5"
    assert f"{_TOKEN} GDS_SIZE 999" in observation.log_text
    assert (observation.gds_present, observation.gds_size) == (True, 9)


def test_remote_sentinel_parser_fails_closed_on_dynamic_log_end_collision(
    tmp_path: Path,
) -> None:
    log_path, gds_path = _remote_scratch(
        tmp_path,
        log=f"ordinary line\n{_TOKEN} LOG_END\nline after collision\n",
    )

    completed = _sh(_poll_command(log_path, gds_path))

    assert completed.returncode == 0
    with pytest.raises(ValueError, match="remote XStream sentinel"):
        streamout._parse_remote_poll_output(completed.stdout, _TOKEN)


def test_remote_sentinel_bounds_long_single_line_and_keeps_completion(
    tmp_path: Path,
) -> None:
    log_path, gds_path = _remote_scratch(
        tmp_path,
        log="X" * (256 * 1024) + "\n" + _SUCCESS_LOG.splitlines()[-1] + "\n",
    )

    completed = _sh(_poll_command(log_path, gds_path))

    assert completed.returncode == 0
    assert len(completed.stdout) <= (128 * 1024) + 4096
    observation = streamout._parse_remote_poll_output(completed.stdout, _TOKEN)
    assert observation.log_tail_truncated is True
    assert len(observation.log_bytes) <= 128 * 1024
    assert streamout.parse_xstream_log(observation.log_text).completed is True


@pytest.mark.parametrize("failed_tail_args", ["-c 131073", "-c 131072", "-n 200"])
def test_remote_sentinel_propagates_each_tail_read_failure(
    tmp_path: Path,
    failed_tail_args: str,
) -> None:
    log_path, gds_path = _remote_scratch(
        tmp_path,
        log="X" * ((128 * 1024) + 512) + "\n" + _SUCCESS_LOG.splitlines()[-1] + "\n",
    )
    real_tail = shutil.which("tail")
    assert real_tail is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_tail = fake_bin / "tail"
    fake_tail.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "$VB_FAIL_TAIL_ARGS" ]; then exit 91; fi\n'
        f'exec {shlex.quote(real_tail)} "$@"\n',
        encoding="utf-8",
    )
    fake_tail.chmod(0o755)

    completed = _sh(
        _poll_command(log_path, gds_path),
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "VB_FAIL_TAIL_ARGS": failed_tail_args,
        },
    )

    assert completed.returncode == 91
    assert f"{_TOKEN} LOG_END" not in completed.stdout
    assert not list(log_path.parent.glob(f".{_TOKEN}.*"))


@pytest.mark.parametrize("digest_tool", ["sha256sum", "shasum", "openssl"])
def test_remote_finalization_sentinel_uses_sha256_fallbacks(
    tmp_path: Path,
    digest_tool: str,
) -> None:
    log_bytes = b"current full log\n"
    gds_bytes = b"current-gds"
    log_path, gds_path = _remote_scratch(tmp_path, log=log_bytes, gds=gds_bytes)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for utility in ("rm", "tail", "wc"):
        utility_path = shutil.which(utility)
        assert utility_path is not None
        (fake_bin / utility).symlink_to(utility_path)
    output = (
        'print("SHA2-256(" + p + ")= " + d)'
        if digest_tool == "openssl"
        else 'print(d + "  " + p)'
    )
    code = (
        "import hashlib, pathlib, sys; p = sys.argv[-1]; "
        "d = hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest(); " + output
    )
    tool = fake_bin / digest_tool
    tool.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(code)} "$@"\n',
        encoding="utf-8",
    )
    tool.chmod(0o755)
    command = _poll_command(log_path, gds_path, include_digests=True)

    completed = _sh(command, env={**os.environ, "PATH": fake_bin.as_posix()})

    assert completed.returncode == 0, completed.stderr
    observation = streamout._parse_remote_poll_output(
        completed.stdout,
        _TOKEN,
        require_digests=True,
    )
    assert observation.log_digest == hashlib.sha256(log_bytes).hexdigest()
    assert observation.gds_digest == hashlib.sha256(gds_bytes).hexdigest()
    assert command.index("sha256sum") < command.index("shasum")
    assert command.index("shasum") < command.index("openssl")


@pytest.mark.parametrize(
    ("payload", "require_digests"),
    [
        ("X" * (140 * 1024), False),
        *(
            (payload, False)
            for payload in (
                "{token} LOG_MISSING\n{token} LOG_MISSING\n{token} GDS_MISSING\n",
                "{token} LOG_SIZE -1\n{token} LOG_BEGIN\n{token} LOG_END\n"
                "{token} GDS_MISSING\n",
                "{token} LOG_SIZE nope\n{token} LOG_BEGIN\n{token} LOG_END\n"
                "{token} GDS_MISSING\n",
                "{token} LOG_MISSING\n{token} LOG_SIZE 0\n{token} LOG_BEGIN\n"
                "{token} LOG_END\n{token} GDS_MISSING\n",
                "{token} LOG_SIZE 0\n{token} LOG_BEGIN\nunterminated\n"
                "{token} GDS_MISSING\n",
                "{token} LOG_MISSING\n{token} GDS_MISSING\n{token} GDS_MISSING\n",
                "{token} LOG_MISSING\n{token} GDS_MISSING\n{token} GDS_SIZE 0\n",
                "{token} LOG_MISSING\n{token} GDS_SIZE -1\n",
                "{token} LOG_MISSING\n{token} GDS_SIZE nope\n",
            )
        ),
        *(
            (payload, True)
            for payload in (
                "{token} LOG_SIZE 1\n{token} LOG_SHA256 nope\n"
                "{token} LOG_BEGIN\nx\n{token} LOG_END\n{token} GDS_MISSING\n",
                "{token} LOG_SIZE 1\n{token} LOG_SHA256 {digest}\n"
                "{token} LOG_SHA256 {digest}\n{token} LOG_BEGIN\nx\n"
                "{token} LOG_END\n{token} GDS_MISSING\n",
                "{token} LOG_MISSING\n{token} LOG_SHA256 {digest}\n"
                "{token} GDS_MISSING\n",
                "{token} LOG_SIZE 1\n{token} LOG_BEGIN\nx\n"
                "{token} LOG_END\n{token} GDS_MISSING\n",
                "{token} LOG_MISSING\n{token} GDS_SHA256 {digest}\n"
                "{token} GDS_MISSING\n",
                "{token} LOG_MISSING\n{token} GDS_SIZE 1\n",
            )
        ),
    ],
)
def test_remote_sentinel_parser_fails_closed_on_malformed_protocol(
    payload: str,
    require_digests: bool,
) -> None:
    with pytest.raises(ValueError, match="remote XStream sentinel"):
        streamout._parse_remote_poll_output(
            payload.format(token=_TOKEN, digest="a" * 64),
            _TOKEN,
            require_digests=require_digests,
        )


@pytest.mark.parametrize("scenario", ["symlink", "wrong_owner", "unsafe_mode", "clean"])
def test_remote_stage_command_secures_private_chain(
    tmp_path: Path,
    scenario: str,
) -> None:
    scratch = tmp_path / "scratch root"
    scratch.mkdir(mode=0o700)
    paths = _remote_paths(
        (scratch / "virtuoso_bridge_secureuser" / "client" / "xstream").as_posix()
    )
    user_dir = Path(paths.owned_root.parent.parent.as_posix())
    environment = os.environ.copy()
    if scenario == "symlink":
        target = tmp_path / "attacker target"
        target.mkdir()
        user_dir.symlink_to(target, target_is_directory=True)
    else:
        user_dir.mkdir(mode=0o755 if scenario == "unsafe_mode" else 0o700)
    if scenario == "wrong_owner":
        fake_bin = tmp_path / "fake-bin"
        fake_bin.mkdir()
        fake_stat = fake_bin / "stat"
        fake_stat.write_text("#!/bin/sh\nprintf '999999 700\\n'\n", encoding="utf-8")
        fake_stat.chmod(0o755)
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

    completed = _sh(streamout._remote_stage_command(paths), env=environment)

    run_dir = Path(paths.run_dir.as_posix())
    if scenario == "clean":
        assert completed.returncode == 0, completed.stderr
        assert "VBXSTREAM_STAGE_CREATED" in completed.stdout
        assert "VBXSTREAM_STAGE_READY" in completed.stdout
        assert run_dir.is_dir()
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o700
                   for path in (user_dir, user_dir / "client", Path(paths.owned_root), run_dir))
        return
    assert completed.returncode != 0
    assert "VBXSTREAM_STAGE_CREATED" not in completed.stdout
    assert not run_dir.exists()
    if scenario == "unsafe_mode":
        assert stat.S_IMODE(user_dir.stat().st_mode) == 0o755


@pytest.mark.parametrize("remove_run", [False, True])
def test_remote_delete_command_revalidates_chain_before_exact_remove(
    tmp_path: Path,
    remove_run: bool,
) -> None:
    scratch = tmp_path / "scratch root"
    scratch.mkdir()
    paths = _remote_paths(
        (scratch / "virtuoso_bridge_secureuser" / "client" / "xstream").as_posix()
    )
    user_dir = Path(paths.owned_root.parent.parent.as_posix())
    user_dir.mkdir(mode=0o700)
    attacker_client = tmp_path / "attacker client"
    run_dir = attacker_client / "xstream" / paths.run_dir.name
    run_dir.mkdir(parents=True, mode=0o700)
    gds_path = run_dir / "output.gds"
    gds_path.write_bytes(b"must-survive")
    (user_dir / "client").symlink_to(attacker_client, target_is_directory=True)

    command = streamout._remote_delete_command(paths, remove_run=remove_run)
    completed = _sh(command)

    assert completed.returncode != 0
    assert gds_path.read_bytes() == b"must-survive"
    exact_target = paths.run_dir if remove_run else paths.gds
    action = "rm -rf" if remove_run else "rm -f"
    assert f"{action} {shlex.quote(exact_target.as_posix())}" in command


# Remote transport: staging and launch.


def test_remote_export_stages_owned_posix_run_and_all_request_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/scratch root")
    monkeypatch.setenv("VB_CLIENT_ID", "client/id")
    stream_map = _write_stream_map(tmp_path / "caller map" / "layers.map")
    runner = _FakeRemoteRunner(user="remote/user")
    client = _FakeRemoteClient(runner, skill_result=_REJECTED)

    result = _run_export(
        client,
        tmp_path / "local outputs" / "top.gds",
        stream_map,
        cleanup_policy="never",
    )

    assert (result.status, result.reason) == (Status.ERROR, Reason.SKILL_ERROR)
    assert (result.remote_files_retained, result.local_run_dir) == (True, None)
    run_dir = result.remote_run_dir
    assert run_dir is not None
    assert re.fullmatch(
        r"/scratch root/virtuoso_bridge_remote_user/client_id/xstream/[0-9a-f]{32}",
        run_dir,
    )
    stage_command = runner.calls[0][0]
    assert "umask 077" in stage_command
    assert f"mkdir -m 700 '{run_dir}'" in stage_command
    assert "whoami" not in [command for command, _timeout in runner.calls]
    assert [(local, remote) for local, remote, _timeout in client.uploads] == [
        (stream_map, f"{run_dir}/stream.map")
    ]
    [(skill, skill_timeout)] = client.skills
    assert 0.0 < skill_timeout <= 2.0
    assert {field: _skill_field(skill, field) for field in _SKILL_FIELD_NAMES} == {
        "library": "demo",
        "topCell": "top",
        "view": "layout",
        "strmFile": f"{run_dir}/output.gds",
        "layerMap": f"{run_dir}/stream.map",
        "logFile": f"{run_dir}/xstream.log",
        "runDir": run_dir,
    }
    assert str(tmp_path) not in skill


def test_remote_export_resolves_username_for_restored_scratch_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_map: Path,
) -> None:
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/tmp")
    monkeypatch.setenv("VB_CLIENT_ID", "laptop")
    runner = _FakeRemoteRunner(
        user="   ",
        whoami=CommandResult(0, "cad/user\n", ""),
    )
    client = _FakeRemoteClient(runner, skill_result=_REJECTED)

    first, second = (
        _run_export(client, tmp_path / name, stream_map, cleanup_policy="never")
        for name in ("first.gds", "second.gds")
    )

    assert first.remote_run_dir is not None
    assert second.remote_run_dir is not None
    assert "/virtuoso_bridge_cad_user/laptop/xstream/" in first.remote_run_dir
    assert first.remote_run_dir != second.remote_run_dir
    assert [command for command, _timeout in runner.calls].count("whoami") == 2
    assert len(client.uploads) == 2


@pytest.mark.parametrize(
    ("fault", "retained", "diagnostic"),
    [
        ("mkdir-status", None, "permission denied"),
        ("mkdir-raises", None, "mkdir transport exploded"),
        ("upload-status", True, "upload remote XStream stream map: scp failed"),
        ("upload-raises", True, "upload transport exploded"),
    ],
)
def test_remote_staging_failure_is_transport_error_without_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_map: Path,
    fault: str,
    retained: bool | None,
    diagnostic: str,
) -> None:
    runner = _FakeRemoteRunner(
        mkdir=CommandResult(1, "", "permission denied")
        if fault == "mkdir-status"
        else None,
    )
    upload_error = VirtuosoResult(status=ExecutionStatus.ERROR, errors=["scp failed"])
    client = _FakeRemoteClient(
        runner,
        upload_result=upload_error if fault == "upload-status" else None,
    )
    if fault == "mkdir-raises":
        real_run_command = runner.run_command

        def fail_mkdir(command: str, timeout: float) -> CommandResult:
            if "VBXSTREAM_STAGE_READY" in command:
                raise OSError("mkdir transport exploded")
            return real_run_command(command, timeout)

        monkeypatch.setattr(runner, "run_command", fail_mkdir)
    elif fault == "upload-raises":
        def fail_upload(*_args: object, **_kwargs: object) -> object:
            raise OSError("upload transport exploded")

        monkeypatch.setattr(client, "upload_file", fail_upload)

    result = _run_export(client, tmp_path / "top.gds", stream_map)

    assert (result.status, result.reason) == (Status.ERROR, Reason.TRANSPORT_ERROR)
    assert result.remote_run_dir is not None
    assert result.remote_files_retained is retained
    assert any(diagnostic in error for error in result.errors)
    assert len(client.uploads) == int(fault == "upload-status")
    assert client.skills == []


def test_remote_prefinal_budget_expiry_before_skill_makes_no_more_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
) -> None:
    runner = _FakeRemoteRunner()
    client = _FakeRemoteClient(runner)
    real_upload = client.upload_file

    def upload_then_expire(*args: Any, **kwargs: Any) -> object:
        response = real_upload(*args, **kwargs)
        clock.now = 3.0
        return response

    monkeypatch.setattr(client, "upload_file", upload_then_expire)

    result = _run_export(client, tmp_path / "top.gds", stream_map)

    assert (result.status, result.reason, result.timed_out) == (
        Status.ERROR,
        Reason.TRANSPORT_ERROR,
        True,
    )
    assert result.remote_files_retained is True
    assert client.skills == []
    [(stage_command, _timeout)] = runner.calls
    assert "VBXSTREAM_STAGE_READY" in stage_command


# Remote transport: polling, download and publication.


def test_remote_export_downloads_log_then_gds_and_publishes_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
) -> None:
    gds_bytes = b"fresh-remote-gds"
    client = _remote_client(gds=gds_bytes)
    output_path = tmp_path / "published" / "top.gds"
    log_path = tmp_path / "diagnostics" / "top.log"
    published = _record_publications(monkeypatch)
    real_read = streamout._read_downloaded_file
    read_labels: list[str] = []

    def record_read(path: Path, *, label: str) -> bytes:
        read_labels.append(label)
        return real_read(path, label=label)

    monkeypatch.setattr(streamout, "_read_downloaded_file", record_read)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert (result.status, result.reason, result.timed_out) == (
        Status.SUCCESS,
        Reason.COMPLETED,
        False,
    )
    assert (result.local_log_path, result.local_gds_path) == (log_path, output_path)
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert output_path.read_bytes() == gds_bytes
    assert result.remote_files_retained is True
    assert published == [log_path, output_path]
    # The GDS is validated by a streamed digest, never read whole into memory.
    assert read_labels == ["log"]
    assert client.download_names == ["xstream.log", "output.gds"]
    for (_remote, local, _timeout), parent in zip(
        client.downloads,
        (log_path.parent, output_path.parent),
    ):
        assert local.parent == parent
        assert re.fullmatch(r"\.vbd-[0-9a-f]{32}\.tmp", local.name)
        assert not local.exists()


def test_remote_poll_timeout_stops_without_call_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    class TimeoutRunner:
        def __init__(self) -> None:
            self.calls: list[float] = []

        def run_command(self, _command: str, timeout: float) -> CommandResult:
            self.calls.append(timeout)
            clock.now += timeout
            raise subprocess.TimeoutExpired("ssh", timeout)

    runner = TimeoutRunner()

    outcome = streamout._poll_remote_artifacts(
        runner,
        _remote_paths(_REMOTE_ROOT),
        _Budget.start(1.0, 0.2, clock=clock),
        0.25,
        should_poll=True,
        launch_indeterminate=True,
        recovery_hook=None,
        warnings=[],
    )

    assert outcome.deadline_expired is True
    assert outcome.staging_error is not None
    assert "timed out" in outcome.staging_error
    assert runner.calls == [pytest.approx(0.8)]
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("marker", "failure"),
    [
        (" GDS_SIZE ", CommandResult(7, "", "remote poll transport failed")),
        ("LOG_SHA256", CommandResult(78, "", "no SHA-256 tool available")),
    ],
    ids=["every-sentinel-fails", "finalization-digest-tool-missing"],
)
def test_remote_sentinel_failure_is_transport_error_and_preserves_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    marker: str,
    failure: CommandResult,
) -> None:
    log_path = tmp_path / "top.log"
    log_path.write_text("old-log\n", encoding="utf-8")
    client = _remote_client(gds=b"remote-gds")
    runner = client.ssh_runner
    real_run_command = runner.run_command

    def run_command(command: str, timeout: float) -> CommandResult:
        if marker in command:
            runner.calls.append((command, timeout))
            return failure
        return real_run_command(command, timeout)

    monkeypatch.setattr(runner, "run_command", run_command)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert (result.status, result.reason) == (Status.ERROR, Reason.TRANSPORT_ERROR)
    assert failure.stderr in "\n".join(result.errors)
    assert (result.local_log_path, result.local_gds_path) == (None, None)
    assert result.remote_files_retained is True
    assert output_path.read_bytes() == _OLD_GDS
    assert log_path.read_text(encoding="utf-8") == "old-log\n"
    assert client.downloads == []
    assert any(
        " LOG_SIZE " in command and "LOG_SHA256" not in command
        for command, _timeout in runner.calls
    )


_REMOTE_RACES = [
    pytest.param(
        _EARLY_TERMINAL_LOG, b"unverified", None,
        (Status.FAILURE, Reason.XSTREAM_FAILURE), _EARLY_TERMINAL_LOG, None, (1, 0),
        id="full-log-terminal-outside-tail-blocks-gds",
    ),
    pytest.param(
        _EARLY_BENIGN_LOG,
        b"remote-gds",
        _serve_then_replace("xstream.log", log_text=_EARLY_TERMINAL_LOG),
        (Status.FAILURE, Reason.XSTREAM_FAILURE), _EARLY_TERMINAL_LOG, None, (None, 0),
        id="same-size-early-log-rewrite-uses-current-full-log",
    ),
    pytest.param(
        _SUCCESS_LOG,
        b"initial-remote-gds",
        _serve_then_replace("output.gds", b"x", log_text=_TERMINAL_LOG, gds=b"x"),
        (Status.FAILURE, Reason.XSTREAM_FAILURE), _TERMINAL_LOG, None, (None, None),
        id="gds-size-race-republishes-terminal-log",
    ),
    *(
        pytest.param(
            _SUCCESS_LOG,
            b"A" * 4096,
            _serve_then_replace("output.gds", served, gds=b"B" * 4096),
            (Status.SUCCESS, Reason.COMPLETED), _SUCCESS_LOG, b"B" * 4096, (None, 2),
            id=f"same-size-gds-rewrite-{name}-download",
        )
        for name, served in (("old", b"A" * 4096), ("torn", b"A" * 2048 + b"B" * 2048))
    ),
    pytest.param(
        _SUCCESS_LOG, b"remote-gds", _churn("xstream.log"),
        (Status.ERROR, Reason.TRANSPORT_ERROR), None, None, (3, 0),
        id="log-churn-is-bounded",
    ),
    pytest.param(
        _SUCCESS_LOG, b"remote-gds", _churn("output.gds"),
        (Status.ERROR, Reason.PUBLICATION_ERROR), _SUCCESS_LOG, None, (1, 3),
        id="gds-churn-is-bounded",
    ),
]


@pytest.mark.parametrize(
    (
        "log_text",
        "gds",
        "on_download",
        "expected",
        "published_log",
        "published_gds",
        "downloads",
    ),
    _REMOTE_RACES,
)
def test_remote_finalization_publishes_only_current_validated_artifacts(
    tmp_path: Path,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    log_text: str,
    gds: bytes,
    on_download: _DownloadHook | None,
    expected: tuple[ExecutionStatus, GdsExportReason],
    published_log: str | None,
    published_gds: bytes | None,
    downloads: tuple[int | None, int | None],
) -> None:
    if log_text == _EARLY_BENIGN_LOG:
        terminal = _EARLY_TERMINAL_LOG
        assert len(log_text.encode()) == len(terminal.encode())
        assert log_text.splitlines()[-200:] == terminal.splitlines()[-200:]
    log_path = tmp_path / "top.log"
    log_path.write_text("old-log\n", encoding="utf-8")
    client = _remote_client(log_text=log_text, gds=gds, on_download=on_download)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert (result.status, result.reason) == expected
    if published_log is None:
        assert result.local_log_path is None
        assert log_path.read_text(encoding="utf-8") == "old-log\n"
    else:
        assert result.local_log_path == log_path
        assert log_path.read_text(encoding="utf-8") == published_log
    assert result.local_gds_path == (output_path if published_gds else None)
    assert output_path.read_bytes() == (published_gds or _OLD_GDS)
    for name, count in zip(("xstream.log", "output.gds"), downloads):
        if count is not None:
            assert client.download_names.count(name) == count
    if result.status == Status.FAILURE:
        assert any("rm -f " in command for command, _ in client.ssh_runner.calls)


def test_remote_log_republish_failure_drops_stale_path_and_keeps_current_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
) -> None:
    log_path = tmp_path / "top.log"
    client = _remote_client(gds=b"remote-gds")
    real_publish = streamout._publish_file
    log_publications: list[Path] = []

    def fail_second_log_publication(
        source: Path,
        destination: Path,
        **kwargs: Any,
    ) -> None:
        if destination == log_path.resolve():
            log_publications.append(destination)
            if len(log_publications) == 2:
                raise OSError("second log publication denied")
        real_publish(source, destination, **kwargs)
        if len(log_publications) == 1:
            client.ssh_runner.log_text = _TERMINAL_LOG

    monkeypatch.setattr(streamout, "_publish_file", fail_second_log_publication)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert (result.status, result.reason) == (Status.ERROR, Reason.PUBLICATION_ERROR)
    assert result.log_result is not None
    assert result.log_result.terminal_failures
    assert (result.local_log_path, result.local_gds_path) == (None, None)
    assert _TERMINAL_LINE in result.errors
    assert any("second log publication denied" in error for error in result.errors)
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert output_path.read_bytes() == _OLD_GDS


def test_remote_validator_budget_expiry_blocks_gds_commit(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
) -> None:
    client = _remote_client(gds=b"fresh-gds")
    runner = client.ssh_runner
    real_run_command = runner.run_command
    sentinel_calls: list[str] = []

    def expire_on_sixth_sentinel(command: str, timeout: float) -> CommandResult:
        result = real_run_command(command, timeout)
        if " LOG_SIZE " in command and " GDS_SIZE " in command:
            sentinel_calls.append(command)
            if len(sentinel_calls) == 6:
                clock.now = 4.0
        return result

    monkeypatch.setattr(runner, "run_command", expire_on_sixth_sentinel)

    result = _run_export(client, output_path, stream_map, cleanup_policy="never")

    assert len(sentinel_calls) == 6
    assert (result.status, result.reason, result.timed_out) == (
        Status.ERROR,
        Reason.TRANSPORT_ERROR,
        True,
    )
    assert result.local_gds_path is None
    assert output_path.read_bytes() == _OLD_GDS


def test_remote_gds_digest_checks_budget_before_each_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gds_path = tmp_path / "download.gds"
    gds_path.write_bytes(b"0123456789")
    monkeypatch.setattr(streamout, "_REMOTE_HASH_CHUNK_BYTES", 4)
    budget_checks: list[bool] = []

    class ExpiringBudget:
        def timeout(self, *, finalizing: bool) -> float:
            budget_checks.append(finalizing)
            if len(budget_checks) == 3:
                raise _BudgetExpired("export time budget exhausted")
            return 1.0

    with pytest.raises(_BudgetExpired, match="budget exhausted"):
        streamout._stream_file_sha256(
            gds_path,
            ExpiringBudget(),  # type: ignore[arg-type]
        )

    assert budget_checks == [True, True, True]


@pytest.mark.parametrize("unlink_fails", [False, True])
def test_remote_download_temp_cleanup_runs_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unlink_fails: bool,
) -> None:
    clock = _FakeClock(0.0)
    budget = _Budget.start(4.0, 1.0, clock=clock)
    temporary = tmp_path / (".vbd-" + ("a" * 32) + ".tmp")
    temporary.write_bytes(b"staged")
    if unlink_fails:
        def fail_unlink(_path: Path) -> None:
            raise OSError("file is locked")

        monkeypatch.setattr(Path, "unlink", fail_unlink)
    clock.now = 4.0
    warnings: list[str] = []

    streamout._cleanup_local_download_temp(temporary, budget, warnings)

    assert temporary.exists() is unlink_fails
    assert warnings == [
        f"local download temp retained: {temporary}: file is locked"
        if unlink_fails
        else f"local download temp removed after export deadline: {temporary}"
    ]


# Remote transport: cleanup.

_OK = CommandResult(0, "", "")


@pytest.mark.parametrize(
    (
        "cleanup_policy",
        "log_text",
        "on_download",
        "cleanup",
        "expected",
        "retained",
        "removes",
    ),
    [
        pytest.param(
            "success", _SUCCESS_LOG, None, _OK,
            (Status.SUCCESS, Reason.COMPLETED), False, True,
            id="success-removed",
        ),
        pytest.param(
            "success", _SUCCESS_LOG, None, CommandResult(1, "", "permission denied"),
            (Status.SUCCESS, Reason.COMPLETED), True, True,
            id="success-cleanup-denied",
        ),
        pytest.param(
            "success", _SUCCESS_LOG, None,
            CommandResult(255, "", "Connection reset by peer"),
            (Status.SUCCESS, Reason.COMPLETED), None, True,
            id="success-cleanup-disconnected",
        ),
        pytest.param(
            "always", _TERMINAL_LOG, None, _OK,
            (Status.FAILURE, Reason.XSTREAM_FAILURE), False, True,
            id="always-after-failed-log",
        ),
        pytest.param(
            "always", _SUCCESS_LOG,
            _fail_download("xstream.log", OSError("log transfer failed")), _OK,
            (Status.ERROR, Reason.TRANSPORT_ERROR), True, False,
            id="always-retains-when-full-log-download-fails",
        ),
        *(
            pytest.param(
                policy, _SUCCESS_LOG,
                _fail_download(
                    "output.gds",
                    VirtuosoResult(
                        status=ExecutionStatus.ERROR,
                        errors=["GDS transfer failed"],
                    ),
                ),
                _OK,
                (Status.ERROR, Reason.TRANSPORT_ERROR), retained, policy == "always",
                id=f"{policy}-after-gds-download-failure",
            )
            for policy, retained in (("never", True), ("always", False))
        ),
    ],
)
def test_remote_cleanup_policy_reports_exact_retention_state(
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
    cleanup_policy: str,
    log_text: str,
    on_download: _DownloadHook | None,
    cleanup: CommandResult,
    expected: tuple[ExecutionStatus, GdsExportReason],
    retained: bool | None,
    removes: bool,
) -> None:
    client = _remote_client(
        log_text=log_text,
        gds=b"remote-gds",
        on_download=on_download,
    )
    client.ssh_runner.cleanup = cleanup

    result = _run_export(client, output_path, stream_map, cleanup_policy=cleanup_policy)

    assert (result.status, result.reason) == expected
    assert result.remote_files_retained is retained
    commands = [command for command, _timeout in client.ssh_runner.calls]
    removals = [command for command in commands if "rm -rf " in command]
    assert len(removals) == int(removes)
    if removes:
        assert f"rm -rf {result.remote_run_dir}" in removals[0]
        assert commands[-1] == removals[0]
    if removes and retained is not False:
        assert any("cleanup" in warning for warning in result.warnings)
    if result.status != Status.SUCCESS:
        assert result.local_gds_path is None
        assert output_path.read_bytes() == _OLD_GDS
    if expected[0] == Status.FAILURE:
        assert any("rm -f " in command for command in commands)
    if on_download is not None and "GDS" in str(result.errors):
        assert result.local_log_path == _default_log(output_path)
        assert any("GDS transfer failed" in error for error in result.errors)


def test_remote_cleanup_budget_exhaustion_sends_no_cleanup_command(
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    stream_map: Path,
    output_path: Path,
) -> None:
    client = _remote_client(gds=b"fresh-gds")
    real_publish = streamout._publish_file

    def expire_after_gds(source: Path, destination: Path, **kwargs: Any) -> None:
        real_publish(source, destination, **kwargs)
        if destination.suffix == ".gds":
            clock.now = 4.0

    monkeypatch.setattr(streamout, "_publish_file", expire_after_gds)

    result = _run_export(client, output_path, stream_map)

    assert result.status == Status.SUCCESS
    assert result.remote_files_retained is True
    assert not any("rm -rf " in command for command, _ in client.ssh_runner.calls)
    assert any("cleanup skipped" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("paths", "cleanup", "warning", "commands"),
    [
        pytest.param(
            _remote_paths(_REMOTE_ROOT, _REMOTE_ROOT.parent), None,
            "refused to clean unowned remote XStream path", 0,
            id="unowned-run-dir",
        ),
        pytest.param(
            _remote_paths("/tmp/safe/../escape/virtuoso_bridge_user/client/xstream"),
            None, "refused to clean unowned remote XStream path", 0,
            id="parent-traversal",
        ),
        pytest.param(
            _remote_paths(_REMOTE_ROOT), PermissionError("cleanup denied"),
            "cleanup failed", 1,
            id="permission-exception",
        ),
    ],
)
def test_remote_cleanup_retains_files_when_unsafe_or_failed(
    paths: streamout._RemoteExportPaths,
    cleanup: object | None,
    warning: str,
    commands: int,
) -> None:
    runner = _FakeRemoteRunner(cleanup=cleanup)
    warnings: list[str] = []

    retained = streamout._cleanup_remote_run(
        runner,
        paths,
        _Budget.start(10.0, 1.0, clock=lambda: 0.0),
        warnings,
    )

    assert retained is True
    assert len(runner.calls) == commands
    assert any(warning in actual for actual in warnings)

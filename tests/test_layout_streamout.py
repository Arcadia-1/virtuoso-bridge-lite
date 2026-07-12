from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import stat
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

_PUBLIC_LAYOUT_STREAMOUT_NAMES = (
    "XStreamExportRequest",
    "XStreamTranslatedStructure",
    "XStreamLogResult",
    "GdsExportReason",
    "GdsExportResult",
    "xstream_export_gds_skill",
    "parse_xstream_log",
    "export_gds",
)


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

    def __init__(
        self,
        execute: Callable[[str, float], object],
        *,
        is_remote: bool = False,
    ) -> None:
        self.is_remote = is_remote
        self._execute = execute
        self.skill_calls: list[tuple[str, float]] = []

    def execute_skill(self, skill_code: str, timeout: float) -> object:
        assert timeout > 0.0
        self.skill_calls.append((skill_code, timeout))
        return self._execute(skill_code, timeout)

    def upload_file(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("local export must not upload files")

    def download_file(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("local export must not download files")

    def dismiss_dialog(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("local export must not use X11 recovery")


class _FakeRemoteRunner:
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
        if "rm -f -- " in command:
            if isinstance(self.discard, BaseException):
                raise self.discard
            assert isinstance(self.discard, CommandResult)
            return self.discard
        if "rm -rf -- " in command:
            if isinstance(self.cleanup, BaseException):
                raise self.cleanup
            assert isinstance(self.cleanup, CommandResult)
            return self.cleanup
        raise AssertionError(f"unexpected remote command: {command}")


class _FakeRemoteClient:
    def __init__(
        self,
        runner: _FakeRemoteRunner,
        *,
        upload_result: object | None = None,
        skill_result: object | None = None,
        remote_log: bytes | None = None,
        remote_gds: bytes | None = None,
    ) -> None:
        self.ssh_runner = runner
        self.upload_result = upload_result or VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
        )
        self.skill_result = skill_result or VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["remote launch rejected"],
        )
        self.remote_log = remote_log
        self.remote_gds = remote_gds
        self.uploads: list[tuple[Path, str, float]] = []
        self.skills: list[tuple[str, float]] = []
        self.downloads: list[tuple[str, Path, float]] = []

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
        if remote_path.endswith("/xstream.log") and self.remote_log is not None:
            Path(local_path).write_bytes(self.remote_log)
        elif remote_path.endswith("/output.gds") and self.remote_gds is not None:
            Path(local_path).write_bytes(self.remote_gds)
        else:
            raise AssertionError("missing remote artifacts must not be downloaded")
        return VirtuosoResult(status=ExecutionStatus.SUCCESS)


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
_TERMINAL_LOG = (
    _PARTIAL_LOG + "INFO (XSTRM-273): Translation failed.\n"
)
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
_ZERO_COUNT_WITH_ERROR_LOG = (
    _PARTIAL_LOG
    + "ERROR: current-run XStream error despite zero completion count\n"
    + "INFO (XSTRM-234): Translation completed. 0 error(s) and "
    "0 warning(s) found.\n"
)
_STARTED_WIRE = '("xstreamRequest" "started" nil nil)'


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
    gds: bytes | None = b"current-run-gds",
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
    gds: bytes | None = b"current-run-gds",
    response: object | None = None,
    is_remote: bool = False,
) -> _FakeLocalClient:
    def execute(skill: str, _timeout: float) -> object:
        _write_artifacts(skill, log_text=log_text, gds=gds)
        return _started_result() if response is None else response

    return _FakeLocalClient(execute, is_remote=is_remote)


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
        "recovery_hook": None,
    }
    options.update(overrides)
    return streamout.export_gds(
        client,
        "demo",
        "top",
        output_path,
        **options,
    )


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


def _log_result(
    *,
    completed: bool = False,
    error_count: int | None = None,
    warning_count: int | None = None,
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


def _remote_test_paths() -> streamout._RemoteExportPaths:
    owned_root = PurePosixPath(
        "/tmp/virtuoso_bridge_remote/client/xstream"
    )
    run_dir = owned_root / ("a" * 32)
    return streamout._RemoteExportPaths(
        owned_root=owned_root,
        run_dir=run_dir,
        gds=run_dir / "output.gds",
        log=run_dir / "xstream.log",
        stream_map=run_dir / "stream.map",
    )


def _remote_paths_under(scratch: Path) -> streamout._RemoteExportPaths:
    owned_root = PurePosixPath(
        (scratch / "virtuoso_bridge_secureuser" / "client" / "xstream").as_posix()
    )
    run_dir = owned_root / ("c" * 32)
    return streamout._RemoteExportPaths(
        owned_root=owned_root,
        run_dir=run_dir,
        gds=run_dir / "output.gds",
        log=run_dir / "xstream.log",
        stream_map=run_dir / "stream.map",
    )


def test_layout_public_api_exports_xstream_streamout_names() -> None:
    for name in _PUBLIC_LAYOUT_STREAMOUT_NAMES:
        assert hasattr(layout_api, name)
        assert name in layout_api.__all__


def test_layout_ops_export_gds_delegates_all_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = object()
    output_path = Path("build/top.gds")
    stream_map = Path("pdk/stream.map")
    log_path = Path("build/top.log")
    recovery_hook = lambda: None
    sentinel = object()
    captured: dict[str, object] = {}

    def fake_export_gds(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(layout_api, "export_gds", fake_export_gds)

    result = layout_api.LayoutOps(owner).export_gds(
        "worklib",
        "top",
        output_path,
        stream_map=stream_map,
        view="maskLayout",
        log_path=log_path,
        timeout=120.0,
        poll_interval=0.25,
        skill_timeout=12.0,
        finalization_reserve=18.0,
        cleanup_policy="never",
        recovery_hook=recovery_hook,
    )

    assert result is sentinel
    assert captured["args"] == (owner, "worklib", "top", output_path)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs == {
        "stream_map": stream_map,
        "view": "maskLayout",
        "log_path": log_path,
        "timeout": 120.0,
        "poll_interval": 0.25,
        "skill_timeout": 12.0,
        "finalization_reserve": 18.0,
        "cleanup_policy": "never",
        "recovery_hook": recovery_hook,
    }
    assert kwargs["recovery_hook"] is recovery_hook


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
    if unlink_fails:
        assert warnings == [
            f"local download temp retained: {temporary}: file is locked"
        ]
    else:
        assert warnings == [
            f"local download temp removed after export deadline: {temporary}"
        ]


def test_remote_sentinel_command_reports_missing_artifacts_with_exit_zero(
    tmp_path: Path,
) -> None:
    log_path = PurePosixPath((tmp_path / "remote scratch" / "xstream.log").as_posix())
    gds_path = PurePosixPath((tmp_path / "remote scratch" / "output.gds").as_posix())
    token = "VBXSTREAM_0123456789abcdef"

    command = streamout._remote_poll_command(log_path, gds_path, token)
    completed = subprocess.run(
        ["sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    observation = streamout._parse_remote_poll_output(completed.stdout, token)
    assert observation == streamout._ArtifactObservation()
    assert "'" + log_path.as_posix() + "'" in command
    assert "'" + gds_path.as_posix() + "'" in command
    assert "sha256sum" not in command
    assert "shasum" not in command
    assert "openssl" not in command


@pytest.mark.parametrize("digest_tool", ["sha256sum", "shasum", "openssl"])
def test_remote_finalization_sentinel_uses_sha256_fallbacks(
    tmp_path: Path,
    digest_tool: str,
) -> None:
    run_dir = tmp_path / "remote scratch"
    run_dir.mkdir()
    log_path = run_dir / "xstream.log"
    gds_path = run_dir / "output.gds"
    log_bytes = b"current full log\n"
    gds_bytes = b"current-gds"
    log_path.write_bytes(log_bytes)
    gds_path.write_bytes(gds_bytes)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for utility in ("rm", "tail", "wc"):
        utility_path = shutil.which(utility)
        assert utility_path is not None
        (fake_bin / utility).symlink_to(utility_path)
    tool = fake_bin / digest_tool
    if digest_tool == "sha256sum":
        script = "#!/bin/sh\nexec /usr/bin/sha256sum \"$@\"\n"
    elif digest_tool == "shasum":
        script = (
            "#!/bin/sh\nshift 3\n"
            "line=$(/usr/bin/sha256sum -- \"$1\") || exit $?\n"
            "printf '%s  %s\\n' \"${line%% *}\" \"$1\"\n"
        )
    else:
        script = (
            "#!/bin/sh\nshift 2\n"
            "line=$(/usr/bin/sha256sum -- \"$1\") || exit $?\n"
            "printf 'SHA2-256(%s)= %s\\n' \"$1\" \"${line%% *}\"\n"
        )
    tool.write_text(script, encoding="utf-8")
    tool.chmod(0o755)
    token = "VBXSTREAM_abcdef0123456789"
    command = streamout._remote_poll_command(
        PurePosixPath(log_path.as_posix()),
        PurePosixPath(gds_path.as_posix()),
        token,
        include_digests=True,
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": fake_bin.as_posix()},
    )

    assert completed.returncode == 0, completed.stderr
    observation = streamout._parse_remote_poll_output(
        completed.stdout,
        token,
        require_digests=True,
    )
    assert observation.log_digest == hashlib.sha256(log_bytes).hexdigest()
    assert observation.gds_digest == hashlib.sha256(gds_bytes).hexdigest()
    assert command.index("sha256sum") < command.index("shasum")
    assert command.index("shasum") < command.index("openssl")


@pytest.mark.parametrize(
    "payload",
    [
        (
            "{token} LOG_SIZE 1\n{token} LOG_SHA256 nope\n"
            "{token} LOG_BEGIN\nx\n{token} LOG_END\n"
            "{token} GDS_MISSING\n"
        ),
        (
            "{token} LOG_SIZE 1\n{token} LOG_SHA256 {digest}\n"
            "{token} LOG_SHA256 {digest}\n{token} LOG_BEGIN\nx\n"
            "{token} LOG_END\n{token} GDS_MISSING\n"
        ),
        (
            "{token} LOG_MISSING\n{token} LOG_SHA256 {digest}\n"
            "{token} GDS_MISSING\n"
        ),
        (
            "{token} LOG_SIZE 1\n{token} LOG_BEGIN\nx\n"
            "{token} LOG_END\n{token} GDS_MISSING\n"
        ),
        (
            "{token} LOG_MISSING\n{token} GDS_SHA256 {digest}\n"
            "{token} GDS_MISSING\n"
        ),
        "{token} LOG_MISSING\n{token} GDS_SIZE 1\n",
    ],
)
def test_remote_finalization_parser_rejects_invalid_digest_protocol(
    payload: str,
) -> None:
    token = "VBXSTREAM_abcdef0123456789"

    with pytest.raises(ValueError, match="remote XStream sentinel"):
        streamout._parse_remote_poll_output(
            payload.format(token=token, digest="a" * 64),
            token,
            require_digests=True,
        )


def test_remote_sentinel_parser_keeps_log_text_inside_random_frame(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "remote scratch"
    run_dir.mkdir()
    log_path = run_dir / "xstream.log"
    gds_path = run_dir / "output.gds"
    token = "VBXSTREAM_abcdef0123456789"
    lines = [f"line {index}" for index in range(205)]
    lines[-2] = f"{token} GDS_SIZE 999"
    log_path.write_text("\n".join(lines), encoding="utf-8")
    gds_path.write_bytes(b"fresh-gds")

    command = streamout._remote_poll_command(
        PurePosixPath(log_path.as_posix()),
        PurePosixPath(gds_path.as_posix()),
        token,
    )
    completed = subprocess.run(
        ["sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    observation = streamout._parse_remote_poll_output(completed.stdout, token)
    assert observation.log_present is True
    assert observation.log_size == log_path.stat().st_size
    assert observation.log_text.splitlines()[0] == "line 5"
    assert f"{token} GDS_SIZE 999" in observation.log_text
    assert observation.gds_present is True
    assert observation.gds_size == len(b"fresh-gds")


def test_remote_sentinel_parser_fails_closed_on_dynamic_log_end_collision(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "remote scratch"
    run_dir.mkdir()
    log_path = run_dir / "xstream.log"
    gds_path = run_dir / "output.gds"
    token = "VBXSTREAM_abcdef0123456789"
    log_path.write_text(
        f"ordinary line\n{token} LOG_END\nline after collision\n",
        encoding="utf-8",
    )
    command = streamout._remote_poll_command(
        PurePosixPath(log_path.as_posix()),
        PurePosixPath(gds_path.as_posix()),
        token,
    )
    completed = subprocess.run(
        ["sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    with pytest.raises(ValueError, match="remote XStream sentinel"):
        streamout._parse_remote_poll_output(completed.stdout, token)


def test_remote_sentinel_bounds_long_single_line_and_keeps_completion(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "remote scratch"
    run_dir.mkdir()
    log_path = run_dir / "xstream.log"
    gds_path = run_dir / "output.gds"
    token = "VBXSTREAM_abcdef0123456789"
    log_text = (
        "X" * (256 * 1024)
        + "\nINFO (XSTRM-234): Translation completed. 0 error(s) and "
        "0 warning(s) found.\n"
    )
    log_path.write_text(log_text, encoding="utf-8")
    command = streamout._remote_poll_command(
        PurePosixPath(log_path.as_posix()),
        PurePosixPath(gds_path.as_posix()),
        token,
    )
    completed = subprocess.run(
        ["sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert len(completed.stdout) <= (128 * 1024) + 4096
    observation = streamout._parse_remote_poll_output(completed.stdout, token)
    assert observation.log_tail_truncated is True
    assert len(observation.log_bytes) <= 128 * 1024
    assert streamout.parse_xstream_log(observation.log_text).completed is True


@pytest.mark.parametrize(
    "failed_tail_args",
    ["-c 131073", "-c 131072", "-n 200"],
)
def test_remote_sentinel_propagates_each_tail_read_failure(
    tmp_path: Path,
    failed_tail_args: str,
) -> None:
    run_dir = tmp_path / "remote scratch"
    run_dir.mkdir()
    log_path = run_dir / "xstream.log"
    gds_path = run_dir / "output.gds"
    log_path.write_text(
        "X" * ((128 * 1024) + 512)
        + "\nINFO (XSTRM-234): Translation completed. 0 error(s) and "
        "0 warning(s) found.\n",
        encoding="utf-8",
    )
    real_tail = shutil.which("tail")
    assert real_tail is not None
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_tail = fake_bin / "tail"
    fake_tail.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "$VB_FAIL_TAIL_ARGS" ]; then exit 91; fi\n'
        f"exec {shlex.quote(real_tail)} \"$@\"\n",
        encoding="utf-8",
    )
    fake_tail.chmod(0o755)
    token = "VBXSTREAM_abcdef0123456789"
    command = streamout._remote_poll_command(
        PurePosixPath(log_path.as_posix()),
        PurePosixPath(gds_path.as_posix()),
        token,
    )

    completed = subprocess.run(
        ["/bin/sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "VB_FAIL_TAIL_ARGS": failed_tail_args,
        },
    )

    assert completed.returncode == 91
    assert f"{token} LOG_END" not in completed.stdout
    assert not list(run_dir.glob(f".{token}.*"))


def test_remote_sentinel_parser_rejects_oversized_stdout_before_split() -> None:
    token = "VBXSTREAM_abcdef0123456789"

    with pytest.raises(ValueError, match="exceeds remote XStream sentinel limit"):
        streamout._parse_remote_poll_output("X" * (140 * 1024), token)


@pytest.mark.parametrize(
    "payload",
    [
        "{token} LOG_MISSING\n{token} LOG_MISSING\n{token} GDS_MISSING\n",
        "{token} LOG_SIZE -1\n{token} LOG_BEGIN\n{token} LOG_END\n{token} GDS_MISSING\n",
        "{token} LOG_SIZE nope\n{token} LOG_BEGIN\n{token} LOG_END\n{token} GDS_MISSING\n",
        "{token} LOG_MISSING\n{token} LOG_SIZE 0\n{token} LOG_BEGIN\n{token} LOG_END\n{token} GDS_MISSING\n",
        "{token} LOG_SIZE 0\n{token} LOG_BEGIN\nunterminated\n{token} GDS_MISSING\n",
        "{token} LOG_MISSING\n{token} GDS_MISSING\n{token} GDS_MISSING\n",
        "{token} LOG_MISSING\n{token} GDS_MISSING\n{token} GDS_SIZE 0\n",
        "{token} LOG_MISSING\n{token} GDS_SIZE -1\n",
        "{token} LOG_MISSING\n{token} GDS_SIZE nope\n",
    ],
)
def test_remote_sentinel_parser_rejects_malformed_control_protocol(
    payload: str,
) -> None:
    token = "VBXSTREAM_abcdef0123456789"

    with pytest.raises(ValueError, match="remote XStream sentinel"):
        streamout._parse_remote_poll_output(payload.format(token=token), token)


def test_remote_export_stages_owned_posix_run_and_all_request_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/scratch root")
    monkeypatch.setenv("VB_CLIENT_ID", "client/id")
    stream_map = _write_stream_map(tmp_path / "caller map" / "layers.map")
    runner = _FakeRemoteRunner(user="remote/user")
    client = _FakeRemoteClient(runner)

    result = _run_export(
        client,
        tmp_path / "local outputs" / "top.gds",
        stream_map,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.SKILL_ERROR
    assert result.remote_files_retained is True
    assert result.local_run_dir is None
    assert result.remote_run_dir is not None
    assert re.fullmatch(
        r"/scratch root/virtuoso_bridge_remote_user/client_id/xstream/[0-9a-f]{32}",
        result.remote_run_dir,
    )
    assert "\\" not in result.remote_run_dir
    assert "umask 077" in runner.calls[0][0]
    assert f"mkdir -m 700 -- '{result.remote_run_dir}'" in runner.calls[0][0]
    assert all(timeout > 0.0 for _command, timeout in runner.calls)
    assert len(client.uploads) == 1
    assert client.uploads[0][0] == stream_map
    assert client.uploads[0][1] == f"{result.remote_run_dir}/stream.map"
    assert len(client.skills) == 1
    skill, skill_timeout = client.skills[0]
    assert 0.0 < skill_timeout <= 2.0
    assert _skill_field(skill, "library") == "demo"
    assert _skill_field(skill, "topCell") == "top"
    assert _skill_field(skill, "view") == "layout"
    assert _skill_field(skill, "strmFile") == f"{result.remote_run_dir}/output.gds"
    assert _skill_field(skill, "layerMap") == f"{result.remote_run_dir}/stream.map"
    assert _skill_field(skill, "logFile") == f"{result.remote_run_dir}/xstream.log"
    assert _skill_field(skill, "runDir") == result.remote_run_dir
    assert str(tmp_path) not in skill
    assert not any(command == "whoami" for command, _timeout in runner.calls)


def test_remote_export_budgeted_whoami_is_sanitized_and_runs_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/tmp")
    monkeypatch.setenv("VB_CLIENT_ID", "laptop")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    runner = _FakeRemoteRunner(
        user="   ",
        whoami=CommandResult(0, "cad/user\n", ""),
    )
    client = _FakeRemoteClient(runner)

    first = _run_export(
        client,
        tmp_path / "first.gds",
        stream_map,
        cleanup_policy="never",
    )
    second = _run_export(
        client,
        tmp_path / "second.gds",
        stream_map,
        cleanup_policy="never",
    )

    assert first.remote_run_dir is not None
    assert second.remote_run_dir is not None
    assert "/virtuoso_bridge_cad_user/laptop/xstream/" in first.remote_run_dir
    assert first.remote_run_dir != second.remote_run_dir
    whoami_calls = [call for call in runner.calls if call[0] == "whoami"]
    assert len(whoami_calls) == 2
    assert all(timeout > 0.0 for _command, timeout in whoami_calls)
    assert len(client.uploads) == 2


def test_remote_export_mkdir_failure_is_transport_error_without_launch(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    runner = _FakeRemoteRunner(
        mkdir=CommandResult(1, "", "permission denied"),
    )
    client = _FakeRemoteClient(runner)

    result = _run_export(client, tmp_path / "top.gds", stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.remote_run_dir is not None
    assert result.remote_files_retained is None
    assert "permission denied" in "\n".join(result.errors)
    assert client.uploads == []
    assert client.skills == []


def test_remote_export_upload_failure_retains_created_run_without_launch(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    runner = _FakeRemoteRunner()
    client = _FakeRemoteClient(
        runner,
        upload_result=VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["scp failed"],
        ),
    )

    result = _run_export(client, tmp_path / "top.gds", stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.remote_run_dir is not None
    assert result.remote_files_retained is True
    assert result.errors == ("failed to upload remote XStream stream map: scp failed",)
    assert len(client.uploads) == 1
    assert client.skills == []


@pytest.mark.parametrize(
    ("stage", "expected_retained"),
    [("mkdir", None), ("upload", True)],
)
def test_remote_staging_exceptions_preserve_retention_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected_retained: bool | None,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    runner = _FakeRemoteRunner()
    client = _FakeRemoteClient(runner)
    if stage == "mkdir":
        real_run_command = runner.run_command

        def fail_mkdir(command: str, timeout: float) -> CommandResult:
            if "VBXSTREAM_STAGE_READY" in command:
                raise OSError("mkdir transport exploded")
            return real_run_command(command, timeout)

        monkeypatch.setattr(runner, "run_command", fail_mkdir)
    else:
        def fail_upload(*_args: object, **_kwargs: object) -> object:
            raise OSError("upload transport exploded")

        monkeypatch.setattr(client, "upload_file", fail_upload)

    result = _run_export(client, tmp_path / "top.gds", stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.remote_run_dir is not None
    assert result.remote_files_retained is expected_retained
    assert "transport exploded" in "\n".join(result.errors)
    assert client.skills == []


@pytest.mark.parametrize("scenario", ["symlink", "wrong_owner", "unsafe_mode"])
def test_remote_stage_command_secures_private_chain(
    tmp_path: Path,
    scenario: str,
) -> None:
    scratch = tmp_path / "scratch root"
    scratch.mkdir()
    paths = _remote_paths_under(scratch)
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

    completed = subprocess.run(
        ["sh", "-c", streamout._remote_stage_command(paths)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    if scenario == "unsafe_mode":
        assert completed.returncode == 0
        assert completed.stdout.splitlines() == [
            "VBXSTREAM_STAGE_CREATED",
            "VBXSTREAM_STAGE_READY",
        ]
        for directory in (
            user_dir,
            user_dir / "client",
            user_dir / "client" / "xstream",
            Path(paths.run_dir.as_posix()),
        ):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    else:
        assert completed.returncode != 0
        assert "VBXSTREAM_STAGE_CREATED" not in completed.stdout
        assert not Path(paths.run_dir.as_posix()).exists()


def test_remote_export_rejects_preexisting_private_chain_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch root"
    scratch.mkdir()
    attacker_target = tmp_path / "attacker target"
    attacker_target.mkdir()
    (scratch / "virtuoso_bridge_secureuser").symlink_to(
        attacker_target,
        target_is_directory=True,
    )
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", scratch.as_posix())
    monkeypatch.setenv("VB_CLIENT_ID", "client")

    class LocalShellRunner:
        user = "secureuser"

        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        def run_command(self, command: str, timeout: float) -> CommandResult:
            self.calls.append((command, timeout))
            completed = subprocess.run(
                ["sh", "-c", command],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return CommandResult(
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )

    runner = LocalShellRunner()
    client = _FakeRemoteClient(runner)  # type: ignore[arg-type]
    stream_map = _write_stream_map(tmp_path / "layers.map")

    result = _run_export(client, tmp_path / "top.gds", stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.remote_files_retained is None
    assert client.uploads == []
    assert client.skills == []
    assert not (attacker_target / "client").exists()


@pytest.mark.parametrize("remove_run", [False, True])
def test_remote_delete_command_revalidates_chain_before_exact_remove(
    tmp_path: Path,
    remove_run: bool,
) -> None:
    scratch = tmp_path / "scratch root"
    scratch.mkdir()
    paths = _remote_paths_under(scratch)
    user_dir = Path(paths.owned_root.parent.parent.as_posix())
    user_dir.mkdir(mode=0o700)
    attacker_client = tmp_path / "attacker client"
    run_dir = attacker_client / "xstream" / paths.run_dir.name
    run_dir.mkdir(parents=True, mode=0o700)
    gds_path = run_dir / "output.gds"
    gds_path.write_bytes(b"must-survive")
    (user_dir / "client").symlink_to(
        attacker_client,
        target_is_directory=True,
    )

    command = streamout._remote_delete_command(paths, remove_run=remove_run)
    completed = subprocess.run(
        ["sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert run_dir.is_dir()
    assert gds_path.read_bytes() == b"must-survive"
    exact_target = paths.run_dir if remove_run else paths.gds
    action = "rm -rf" if remove_run else "rm -f"
    assert f"{action} -- {shlex.quote(exact_target.as_posix())}" in command


def test_remote_export_downloads_log_before_gds_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    gds_bytes = b"fresh-remote-gds"
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=gds_bytes)
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_log=_SUCCESS_LOG.encode("utf-8"),
        remote_gds=gds_bytes,
    )
    output_path = tmp_path / "published" / "top.gds"
    log_path = tmp_path / "diagnostics" / "top.log"

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.timed_out is False
    assert result.local_log_path == log_path
    assert result.local_gds_path == output_path
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert output_path.read_bytes() == gds_bytes
    assert result.remote_files_retained is True
    assert [Path(remote).name for remote, _local, _timeout in client.downloads] == [
        "xstream.log",
        "output.gds",
    ]
    assert client.downloads[0][1].parent == log_path.parent
    assert client.downloads[1][1].parent == output_path.parent
    assert all(
        re.fullmatch(r"\.vbd-[0-9a-f]{32}\.tmp", local.name)
        for _remote, local, _timeout in client.downloads
    )
    assert all(timeout > 0.0 for _remote, _local, timeout in client.downloads)


def test_remote_export_polls_generic_error_until_zero_count_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    gds_bytes = b"fresh-remote-gds-after-poll"
    runner = _FakeRemoteRunner(log_text=_PARTIAL_WITH_ERROR_LOG, gds=None)

    def finish(_seconds: float) -> None:
        runner.log_text = _ZERO_COUNT_WITH_ERROR_LOG
        runner.gds = gds_bytes

    clock = _FakeClock(0.0, on_sleep=finish)
    _use_fake_time(monkeypatch, clock)
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_log=_ZERO_COUNT_WITH_ERROR_LOG.encode("utf-8"),
        remote_gds=gds_bytes,
    )
    output_path = tmp_path / "top.gds"

    result = _run_export(
        client,
        output_path,
        stream_map,
        poll_interval=0.25,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.local_gds_path == output_path
    assert result.log_result is not None
    assert result.log_result.errors == (
        "ERROR: current-run XStream error despite zero completion count",
    )
    assert result.errors == result.log_result.errors
    assert output_path.read_bytes() == gds_bytes
    assert clock.sleeps == [0.25]


@pytest.mark.parametrize(
    ("cleanup_policy", "expected_retained"),
    [("never", True), ("always", False)],
)
def test_remote_gds_download_failure_preserves_old_output_and_retention(
    tmp_path: Path,
    cleanup_policy: str,
    expected_retained: bool,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=b"remote-gds")

    class FailingGdsClient(_FakeRemoteClient):
        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: float,
        ) -> object:
            if remote_path.endswith("/output.gds"):
                self.downloads.append((remote_path, Path(local_path), timeout))
                return VirtuosoResult(
                    status=ExecutionStatus.ERROR,
                    errors=["GDS transfer failed"],
                )
            return super().download_file(
                remote_path,
                local_path,
                timeout=timeout,
            )

    client = FailingGdsClient(
        runner,
        skill_result=_started_result(),
        remote_log=_SUCCESS_LOG.encode("utf-8"),
    )
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy=cleanup_policy,
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.local_log_path == log_path
    assert result.local_gds_path is None
    assert output_path.read_bytes() == b"old-gds"
    assert result.remote_files_retained is expected_retained
    assert any("GDS transfer failed" in error for error in result.errors)


def test_remote_export_full_log_overrides_successful_tail_and_blocks_gds(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    full_log = (
        _PARTIAL_LOG
        + "INFO (XSTRM-273): Translation failed.\n"
        + "\n".join(f"detail {index}" for index in range(210))
        + "\nINFO (XSTRM-234): Translation completed. 0 error(s) and "
        "0 warning(s) found.\n"
    )
    gds_bytes = b"unverified-remote-gds"
    runner = _FakeRemoteRunner(log_text=full_log, gds=gds_bytes)
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_log=full_log.encode("utf-8"),
        remote_gds=gds_bytes,
    )
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")
    log_path = tmp_path / "top.log"

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.local_log_path == log_path
    assert result.local_gds_path is None
    assert log_path.read_text(encoding="utf-8") == full_log
    assert output_path.read_bytes() == b"old-gds"
    assert [Path(remote).name for remote, _local, _timeout in client.downloads] == [
        "xstream.log"
    ]
    assert any(
        "rm -f -- " in command
        for command, _timeout in runner.calls
    )


def test_remote_log_republish_failure_drops_stale_path_and_keeps_current_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=b"remote-gds")

    class CurrentLogClient(_FakeRemoteClient):
        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: float,
        ) -> object:
            assert timeout > 0.0
            self.downloads.append((remote_path, Path(local_path), timeout))
            if remote_path.endswith("/xstream.log"):
                assert runner.log_text is not None
                Path(local_path).write_text(runner.log_text, encoding="utf-8")
            else:
                assert runner.gds is not None
                Path(local_path).write_bytes(runner.gds)
            return VirtuosoResult(status=ExecutionStatus.SUCCESS)

    client = CurrentLogClient(runner, skill_result=_started_result())
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    real_publish = streamout._publish_file
    log_publications = 0

    def fail_second_log_publication(
        source: Path,
        destination: Path,
        **kwargs: Any,
    ) -> None:
        nonlocal log_publications
        if destination == log_path.resolve():
            log_publications += 1
            if log_publications == 2:
                raise OSError("second log publication denied")
        real_publish(source, destination, **kwargs)
        if destination == log_path.resolve() and log_publications == 1:
            runner.log_text = _TERMINAL_LOG

    monkeypatch.setattr(
        streamout,
        "_publish_file",
        fail_second_log_publication,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.log_result is not None
    assert result.log_result.terminal_failures
    assert result.local_log_path is None
    assert result.local_gds_path is None
    assert "INFO (XSTRM-273): Translation failed." in result.errors
    assert any("second log publication denied" in error for error in result.errors)
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert output_path.read_bytes() == b"old-gds"


def test_remote_poll_exact_timeout_recovers_once_after_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    monkeypatch.setattr(streamout, "_SLEEP", clock.sleep)
    runner = _FakeRemoteRunner(log_text=_PARTIAL_LOG)
    budget = _Budget.start(1.0, 0.2, clock=clock)
    recovery_calls: list[str] = []
    warnings: list[str] = []

    outcome = streamout._poll_remote_artifacts(
        runner,
        _remote_test_paths(),
        budget,
        0.25,
        should_poll=True,
        launch_indeterminate=True,
        recovery_hook=lambda: recovery_calls.append("recover"),
        warnings=warnings,
    )

    assert outcome.deadline_expired is True
    assert outcome.saw_evidence is True
    assert outcome.log is not None
    assert outcome.log.completed is False
    assert recovery_calls == ["recover"]
    assert len(runner.calls) == 4
    assert all(timeout > 0.0 for _command, timeout in runner.calls)
    assert warnings == []


def test_remote_poll_timeout_stops_without_call_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    monkeypatch.setattr(streamout, "_SLEEP", clock.sleep)

    class TimeoutRunner:
        def __init__(self) -> None:
            self.calls: list[float] = []

        def run_command(self, _command: str, timeout: float) -> CommandResult:
            assert timeout > 0.0
            self.calls.append(timeout)
            clock.now += timeout
            raise subprocess.TimeoutExpired("ssh", timeout)

    runner = TimeoutRunner()
    budget = _Budget.start(1.0, 0.2, clock=clock)

    outcome = streamout._poll_remote_artifacts(
        runner,
        _remote_test_paths(),
        budget,
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


def test_remote_nonzero_poll_is_transport_error_and_preserves_old_outputs(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")

    class NonzeroPollRunner(_FakeRemoteRunner):
        def run_command(self, command: str, timeout: float) -> CommandResult:
            if " LOG_SIZE " in command and " GDS_SIZE " in command:
                assert timeout > 0.0
                self.calls.append((command, timeout))
                return CommandResult(7, "", "remote poll transport failed")
            return super().run_command(command, timeout)

    runner = NonzeroPollRunner()
    client = _FakeRemoteClient(runner, skill_result=_started_result())
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    log_path.write_text("old-log\n", encoding="utf-8")

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert "remote poll transport failed" in "\n".join(result.errors)
    assert result.local_log_path is None
    assert result.local_gds_path is None
    assert result.remote_files_retained is True
    assert output_path.read_bytes() == b"old-gds"
    assert log_path.read_text(encoding="utf-8") == "old-log\n"
    assert client.downloads == []


def test_remote_missing_sha256_tools_is_transport_error(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")

    class MissingDigestRunner(_FakeRemoteRunner):
        def run_command(self, command: str, timeout: float) -> CommandResult:
            if "LOG_SHA256" in command:
                assert timeout > 0.0
                self.calls.append((command, timeout))
                return CommandResult(78, "", "no SHA-256 tool available")
            return super().run_command(command, timeout)

    runner = MissingDigestRunner(log_text=_SUCCESS_LOG, gds=b"remote-gds")
    client = _FakeRemoteClient(runner, skill_result=_started_result())
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert "no SHA-256 tool available" in "\n".join(result.errors)
    assert result.local_gds_path is None
    assert output_path.read_bytes() == b"old-gds"
    assert client.downloads == []
    normal_polls = [
        command
        for command, _timeout in runner.calls
        if " LOG_SIZE " in command and "LOG_SHA256" not in command
    ]
    assert normal_polls


def test_remote_non_timeout_skill_error_observes_once_without_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    runner = _FakeRemoteRunner()
    client = _FakeRemoteClient(runner)

    result = _run_export(
        client,
        tmp_path / "top.gds",
        stream_map,
        cleanup_policy="never",
    )

    sentinel_calls = [
        command
        for command, _timeout in runner.calls
        if " LOG_SIZE " in command and " GDS_SIZE " in command
    ]
    assert result.reason == GdsExportReason.SKILL_ERROR
    assert len(sentinel_calls) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("cleanup_result", "expected_retained"),
    [
        (CommandResult(0, "", ""), False),
        (CommandResult(1, "", "permission denied"), True),
        (CommandResult(255, "", "Connection reset by peer"), None),
    ],
)
def test_remote_success_cleanup_reports_exact_retention_state(
    tmp_path: Path,
    cleanup_result: CommandResult,
    expected_retained: bool | None,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    gds_bytes = b"fresh-gds"
    runner = _FakeRemoteRunner(
        log_text=_SUCCESS_LOG,
        gds=gds_bytes,
        cleanup=cleanup_result,
    )
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_log=_SUCCESS_LOG.encode("utf-8"),
        remote_gds=gds_bytes,
    )

    result = _run_export(client, tmp_path / "top.gds", stream_map)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.remote_files_retained is expected_retained
    cleanup_calls = [
        command
        for command, _timeout in runner.calls
        if "rm -rf -- " in command
    ]
    assert len(cleanup_calls) == 1
    assert f"rm -rf -- {result.remote_run_dir}" in cleanup_calls[0]
    if cleanup_result.returncode != 0:
        assert any("cleanup" in warning for warning in result.warnings)


def test_remote_always_cleanup_runs_after_failed_log_and_preserves_reason(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    gds_bytes = b"unverified-gds"
    runner = _FakeRemoteRunner(log_text=_TERMINAL_LOG, gds=gds_bytes)
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_log=_TERMINAL_LOG.encode("utf-8"),
        remote_gds=gds_bytes,
    )

    result = _run_export(
        client,
        tmp_path / "top.gds",
        stream_map,
        cleanup_policy="always",
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.remote_files_retained is False
    commands = [command for command, _timeout in runner.calls]
    assert any("rm -f -- " in command for command in commands)
    assert f"rm -rf -- {result.remote_run_dir}" in commands[-1]


def test_remote_always_cleanup_retains_run_when_full_log_download_fails(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=b"remote-gds")
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_gds=b"remote-gds",
    )

    result = _run_export(
        client,
        tmp_path / "top.gds",
        stream_map,
        cleanup_policy="always",
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.remote_files_retained is True
    assert not any(
        "rm -rf -- " in command
        for command, _timeout in runner.calls
    )


def test_remote_cleanup_budget_exhaustion_sends_no_cleanup_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    gds_bytes = b"fresh-gds"
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=gds_bytes)
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_log=_SUCCESS_LOG.encode("utf-8"),
        remote_gds=gds_bytes,
    )
    real_publish = streamout._publish_file

    def expire_after_gds(source: Path, destination: Path, **kwargs: Any) -> None:
        real_publish(source, destination, **kwargs)
        if destination.suffix == ".gds":
            clock.now = 4.0

    monkeypatch.setattr(streamout, "_publish_file", expire_after_gds)

    result = _run_export(client, tmp_path / "top.gds", stream_map)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.remote_files_retained is True
    assert not any(
        "rm -rf -- " in command
        for command, _timeout in runner.calls
    )
    assert any("cleanup skipped" in warning for warning in result.warnings)


def test_remote_cleanup_refuses_unowned_path_without_command() -> None:
    paths = _remote_test_paths()
    unowned = streamout._RemoteExportPaths(
        owned_root=paths.owned_root,
        run_dir=paths.owned_root.parent,
        gds=paths.owned_root.parent / "output.gds",
        log=paths.owned_root.parent / "xstream.log",
        stream_map=paths.owned_root.parent / "stream.map",
    )
    runner = _FakeRemoteRunner()
    budget = _Budget.start(10.0, 1.0, clock=lambda: 0.0)
    warnings: list[str] = []

    retained = streamout._cleanup_remote_run(
        runner,
        unowned,
        budget,
        warnings,
    )

    assert retained is True
    assert runner.calls == []
    assert warnings == ["refused to clean unowned remote XStream path"]


def test_remote_cleanup_refuses_parent_traversal_without_command() -> None:
    owned_root = PurePosixPath(
        "/tmp/safe/../escape/virtuoso_bridge_user/client/xstream"
    )
    run_dir = owned_root / ("b" * 32)
    paths = streamout._RemoteExportPaths(
        owned_root=owned_root,
        run_dir=run_dir,
        gds=run_dir / "output.gds",
        log=run_dir / "xstream.log",
        stream_map=run_dir / "stream.map",
    )
    runner = _FakeRemoteRunner()
    budget = _Budget.start(10.0, 1.0, clock=lambda: 0.0)
    warnings: list[str] = []

    retained = streamout._cleanup_remote_run(
        runner,
        paths,
        budget,
        warnings,
    )

    assert retained is True
    assert runner.calls == []
    assert warnings == ["refused to clean unowned remote XStream path"]


def test_remote_cleanup_permission_exception_means_files_retained() -> None:
    runner = _FakeRemoteRunner(cleanup=PermissionError("cleanup denied"))
    budget = _Budget.start(10.0, 1.0, clock=lambda: 0.0)
    warnings: list[str] = []

    retained = streamout._cleanup_remote_run(
        runner,
        _remote_test_paths(),
        budget,
        warnings,
    )

    assert retained is True
    assert any("cleanup failed" in warning for warning in warnings)


def test_remote_gds_size_race_republishes_changed_terminal_log_first(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    initial_gds = b"initial-remote-gds"
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=initial_gds)

    class RacingClient(_FakeRemoteClient):
        def __init__(self) -> None:
            super().__init__(runner, skill_result=_started_result())
            self.gds_downloads = 0

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: float,
        ) -> object:
            assert timeout > 0.0
            self.downloads.append((remote_path, Path(local_path), timeout))
            if remote_path.endswith("/xstream.log"):
                assert runner.log_text is not None
                Path(local_path).write_text(runner.log_text, encoding="utf-8")
            else:
                self.gds_downloads += 1
                if self.gds_downloads == 1:
                    runner.log_text = _TERMINAL_LOG
                    runner.gds = b"x"
                assert runner.gds is not None
                Path(local_path).write_bytes(runner.gds)
            return VirtuosoResult(status=ExecutionStatus.SUCCESS)

    client = RacingClient()
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.local_gds_path is None
    assert output_path.read_bytes() == b"old-gds"
    assert log_path.read_text(encoding="utf-8") == _TERMINAL_LOG


@pytest.mark.parametrize(
    (
        "artifact",
        "expected_reason",
        "expected_log_downloads",
        "expected_gds_downloads",
    ),
    [
        ("log", GdsExportReason.TRANSPORT_ERROR, 3, 0),
        ("gds", GdsExportReason.PUBLICATION_ERROR, 1, 3),
    ],
)
def test_remote_artifact_churn_is_bounded_without_old_gds_replacement(
    tmp_path: Path,
    artifact: str,
    expected_reason: GdsExportReason,
    expected_log_downloads: int,
    expected_gds_downloads: int,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=b"remote-gds")

    class ChurningClient(_FakeRemoteClient):
        def __init__(self) -> None:
            super().__init__(runner, skill_result=_started_result())
            self.log_generation = 0

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: float,
        ) -> object:
            self.downloads.append((remote_path, Path(local_path), timeout))
            if remote_path.endswith("/xstream.log"):
                assert runner.log_text is not None
                Path(local_path).write_text(runner.log_text, encoding="utf-8")
                if artifact == "log":
                    self.log_generation += 1
                    runner.log_text = (
                        _SUCCESS_LOG
                        + f"remote generation {self.log_generation}\n"
                    )
            else:
                assert runner.gds is not None
                Path(local_path).write_bytes(runner.gds)
                if artifact == "gds":
                    runner.gds += b"x"
            return VirtuosoResult(status=ExecutionStatus.SUCCESS)

    client = ChurningClient()
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    log_path.write_text("old-log\n", encoding="utf-8")

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    downloaded_names = [
        Path(remote_path).name
        for remote_path, _local_path, _timeout in client.downloads
    ]
    assert result.status == ExecutionStatus.ERROR
    assert result.reason == expected_reason
    assert result.local_gds_path is None
    assert output_path.read_bytes() == b"old-gds"
    assert downloaded_names.count("xstream.log") == expected_log_downloads
    assert downloaded_names.count("output.gds") == expected_gds_downloads
    if artifact == "log":
        assert result.local_log_path is None
        assert log_path.read_text(encoding="utf-8") == "old-log\n"
    else:
        assert result.local_log_path == log_path
        assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG


def test_remote_same_size_early_log_rewrite_uses_current_full_log(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    terminal_line = "INFO (XSTRM-273): Translation failed."
    benign_line = "ordinary early diagnostic".ljust(len(terminal_line))
    suffix = (
        "\n".join(f"detail {index:03d}" for index in range(220))
        + "\nINFO (XSTRM-234): Translation completed. 0 error(s) and "
        "0 warning(s) found.\n"
    )
    first_log = _PARTIAL_LOG + benign_line + "\n" + suffix
    current_log = _PARTIAL_LOG + terminal_line + "\n" + suffix
    assert len(first_log.encode("utf-8")) == len(current_log.encode("utf-8"))
    assert first_log.splitlines()[-200:] == current_log.splitlines()[-200:]
    runner = _FakeRemoteRunner(log_text=first_log, gds=b"remote-gds")

    class RewritingLogClient(_FakeRemoteClient):
        def __init__(self) -> None:
            super().__init__(runner, skill_result=_started_result())
            self.log_downloads = 0

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: float,
        ) -> object:
            self.downloads.append((remote_path, Path(local_path), timeout))
            if remote_path.endswith("/xstream.log"):
                self.log_downloads += 1
                assert runner.log_text is not None
                Path(local_path).write_text(runner.log_text, encoding="utf-8")
                if self.log_downloads == 1:
                    runner.log_text = current_log
            else:
                assert runner.gds is not None
                Path(local_path).write_bytes(runner.gds)
            return VirtuosoResult(status=ExecutionStatus.SUCCESS)

    client = RewritingLogClient()
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.local_gds_path is None
    assert log_path.read_text(encoding="utf-8") == current_log
    assert output_path.read_bytes() == b"old-gds"


@pytest.mark.parametrize("downloaded_version", ["old", "torn"])
def test_remote_same_size_gds_rewrite_publishes_current_content(
    tmp_path: Path,
    downloaded_version: str,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    first_gds = b"A" * 4096
    current_gds = b"B" * 4096
    torn_gds = first_gds[:2048] + current_gds[2048:]
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=first_gds)

    class RewritingGdsClient(_FakeRemoteClient):
        def __init__(self) -> None:
            super().__init__(runner, skill_result=_started_result())
            self.gds_downloads = 0

        def download_file(
            self,
            remote_path: str,
            local_path: Path,
            *,
            timeout: float,
        ) -> object:
            self.downloads.append((remote_path, Path(local_path), timeout))
            if remote_path.endswith("/xstream.log"):
                assert runner.log_text is not None
                Path(local_path).write_text(runner.log_text, encoding="utf-8")
            else:
                self.gds_downloads += 1
                if self.gds_downloads == 1:
                    Path(local_path).write_bytes(
                        first_gds if downloaded_version == "old" else torn_gds
                    )
                    runner.gds = current_gds
                else:
                    assert runner.gds is not None
                    Path(local_path).write_bytes(runner.gds)
            return VirtuosoResult(status=ExecutionStatus.SUCCESS)

    client = RewritingGdsClient()
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.local_gds_path == output_path
    assert output_path.read_bytes() == current_gds
    assert client.gds_downloads == 2


def test_remote_gds_publication_failure_keeps_paths_and_old_gds_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    gds_bytes = b"remote-gds"
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=gds_bytes)
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_log=_SUCCESS_LOG.encode("utf-8"),
        remote_gds=gds_bytes,
    )
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    log_path.write_text("old-log\n", encoding="utf-8")
    real_publish = streamout._publish_file

    def fail_selected_publication(
        source: Path,
        destination: Path,
        **kwargs: Any,
    ) -> None:
        if destination == output_path.resolve():
            raise OSError("GDS publication denied")
        real_publish(source, destination, **kwargs)

    monkeypatch.setattr(
        streamout,
        "_publish_file",
        fail_selected_publication,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    downloaded_names = [
        Path(remote_path).name
        for remote_path, _local_path, _timeout in client.downloads
    ]
    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.local_gds_path is None
    assert output_path.read_bytes() == b"old-gds"
    assert result.remote_files_retained is True
    assert any("GDS publication denied" in error for error in result.errors)
    assert result.local_log_path == log_path
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert downloaded_names == ["xstream.log", "output.gds"]


def test_remote_validator_budget_expiry_blocks_gds_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    gds_bytes = b"fresh-gds"
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)

    class DeadlineRunner(_FakeRemoteRunner):
        def __init__(self) -> None:
            super().__init__(log_text=_SUCCESS_LOG, gds=gds_bytes)
            self.sentinel_calls = 0

        def run_command(self, command: str, timeout: float) -> CommandResult:
            result = super().run_command(command, timeout)
            if " LOG_SIZE " in command and " GDS_SIZE " in command:
                self.sentinel_calls += 1
                if self.sentinel_calls == 6:
                    clock.now = 4.0
            return result

    runner = DeadlineRunner()
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_log=_SUCCESS_LOG.encode("utf-8"),
        remote_gds=gds_bytes,
    )
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="never",
    )

    assert runner.sentinel_calls == 6
    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.timed_out is True
    assert result.local_gds_path is None
    assert output_path.read_bytes() == b"old-gds"


def test_remote_prefinal_budget_expiry_before_skill_makes_no_more_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    runner = _FakeRemoteRunner()

    class ExpiringUploadClient(_FakeRemoteClient):
        def upload_file(
            self,
            local_path: Path,
            remote_path: str,
            *,
            timeout: float,
        ) -> object:
            response = super().upload_file(
                local_path,
                remote_path,
                timeout=timeout,
            )
            clock.now = 3.0
            return response

    client = ExpiringUploadClient(runner)

    result = _run_export(client, tmp_path / "top.gds", stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.timed_out is True
    assert result.remote_files_retained is True
    assert client.skills == []
    assert len(runner.calls) == 1
    assert "VBXSTREAM_STAGE_READY" in runner.calls[0][0]


def test_remote_gds_validation_uses_stat_without_full_file_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    gds_bytes = b"fresh-gds"
    runner = _FakeRemoteRunner(log_text=_SUCCESS_LOG, gds=gds_bytes)
    client = _FakeRemoteClient(
        runner,
        skill_result=_started_result(),
        remote_log=_SUCCESS_LOG.encode("utf-8"),
        remote_gds=gds_bytes,
    )
    real_read = streamout._read_downloaded_file
    read_labels: list[str] = []

    def reject_gds_read(path: Path, *, label: str) -> bytes:
        read_labels.append(label)
        if label == "GDS":
            raise AssertionError("GDS validation must not read full contents")
        return real_read(path, label=label)

    monkeypatch.setattr(streamout, "_read_downloaded_file", reject_gds_read)

    result = _run_export(
        client,
        tmp_path / "top.gds",
        stream_map,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert read_labels == ["log"]


def test_remote_gds_digest_checks_budget_before_each_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gds_path = tmp_path / "download.gds"
    gds_path.write_bytes(b"0123456789")
    monkeypatch.setattr(streamout, "_REMOTE_HASH_CHUNK_BYTES", 4)

    class ExpiringBudget:
        def __init__(self) -> None:
            self.calls = 0

        def timeout(self, *, finalizing: bool) -> float:
            assert finalizing is True
            self.calls += 1
            if self.calls == 3:
                raise _BudgetExpired("export time budget exhausted")
            return 1.0

    budget = ExpiringBudget()

    with pytest.raises(_BudgetExpired, match="budget exhausted"):
        streamout._stream_file_sha256(gds_path, budget)

    assert budget.calls == 3


@pytest.mark.parametrize(
    (
        "log_text",
        "gds_bytes",
        "skill_result",
        "expected_status",
        "expected_reason",
        "expected_timed_out",
    ),
    [
        (
            _SUCCESS_LOG,
            None,
            _started_result(),
            ExecutionStatus.PARTIAL,
            GdsExportReason.MISSING_GDS,
            True,
        ),
        (
            _SUCCESS_LOG,
            b"",
            _started_result(),
            ExecutionStatus.PARTIAL,
            GdsExportReason.EMPTY_GDS,
            True,
        ),
        (
            _PARTIAL_LOG,
            None,
            _started_result(),
            ExecutionStatus.PARTIAL,
            GdsExportReason.INCOMPLETE_LOG,
            True,
        ),
        (
            _MALFORMED_LOG,
            b"invalid-gds",
            _started_result(),
            ExecutionStatus.ERROR,
            GdsExportReason.MALFORMED_LOG,
            False,
        ),
        (
            _ERROR_COMPLETION_LOG,
            b"invalid-gds",
            _started_result(),
            ExecutionStatus.FAILURE,
            GdsExportReason.XSTREAM_ERRORS,
            False,
        ),
        (
            _ZERO_COUNT_WITH_ERROR_LOG,
            b"diagnostic-gds",
            _started_result(),
            ExecutionStatus.SUCCESS,
            GdsExportReason.COMPLETED,
            False,
        ),
        (
            _TERMINAL_LOG,
            b"invalid-gds",
            _started_result(),
            ExecutionStatus.FAILURE,
            GdsExportReason.XSTREAM_FAILURE,
            False,
        ),
        (
            _SUCCESS_LOG,
            b"blocked-gds",
            VirtuosoResult(
                status=ExecutionStatus.SUCCESS,
                output=(
                    '("xstreamRequest" "started" nil '
                    '("failed to restore XStream field runDir"))'
                ),
            ),
            ExecutionStatus.ERROR,
            GdsExportReason.REQUEST_CLEANUP_ERROR,
            False,
        ),
        (
            _SUCCESS_LOG,
            b"blocked-gds",
            VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["daemon rejected launch"],
            ),
            ExecutionStatus.ERROR,
            GdsExportReason.SKILL_ERROR,
            False,
        ),
        (
            None,
            None,
            VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["Socket timeout after 2s"],
            ),
            ExecutionStatus.PARTIAL,
            GdsExportReason.LAUNCH_INDETERMINATE,
            True,
        ),
        (
            _SUCCESS_LOG,
            b"recovered-gds",
            VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["SKILL execution timeout in Virtuoso"],
                warnings=["bridge warning"],
            ),
            ExecutionStatus.SUCCESS,
            GdsExportReason.COMPLETED,
            False,
        ),
    ],
)
def test_remote_export_preserves_exact_reason_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_text: str | None,
    gds_bytes: bytes | None,
    skill_result: VirtuosoResult,
    expected_status: ExecutionStatus,
    expected_reason: GdsExportReason,
    expected_timed_out: bool,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    runner = _FakeRemoteRunner(log_text=log_text, gds=gds_bytes)
    client = _FakeRemoteClient(
        runner,
        skill_result=skill_result,
        remote_log=(
            log_text.encode("utf-8") if log_text is not None else None
        ),
        remote_gds=gds_bytes,
    )

    result = _run_export(
        client,
        tmp_path / "top.gds",
        stream_map,
        cleanup_policy="never",
    )

    assert result.status == expected_status
    assert result.reason == expected_reason
    assert result.timed_out is expected_timed_out
    if expected_reason in {
        GdsExportReason.REQUEST_CLEANUP_ERROR,
        GdsExportReason.SKILL_ERROR,
        GdsExportReason.MALFORMED_LOG,
        GdsExportReason.XSTREAM_ERRORS,
        GdsExportReason.XSTREAM_FAILURE,
    }:
        assert result.local_gds_path is None
    if "bridge warning" in skill_result.warnings:
        assert "bridge warning" in result.warnings


def test_gds_export_reason_has_exact_string_values_and_public_exports() -> None:
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
    assert streamout.__all__ == [
        "GdsExportReason",
        "GdsExportResult",
        "export_gds",
    ]


def test_gds_export_result_is_frozen_with_exact_tuple_fields() -> None:
    log = _log_result(completed=True, error_count=0, warning_count=1)
    result = GdsExportResult(
        status=ExecutionStatus.FAILURE,
        reason=GdsExportReason.XSTREAM_ERRORS,
        timed_out=False,
        library="demo",
        cell="top",
        view="layout",
        execution_time=1.25,
        local_gds_path=Path("top.gds"),
        local_log_path=Path("top.xstream.log"),
        log_result=log,
        errors=("XSTRM error",),
        warnings=("XSTRM warning",),
        remote_run_dir="/tmp/run",
        local_run_dir=Path("run"),
        remote_files_retained=True,
    )

    assert tuple(field.name for field in fields(GdsExportResult)) == (
        _GDS_EXPORT_RESULT_FIELDS
    )
    assert result.errors == ("XSTRM error",)
    assert result.warnings == ("XSTRM warning",)
    assert isinstance(result.errors, tuple)
    assert isinstance(result.warnings, tuple)
    with pytest.raises(FrozenInstanceError):
        result.cell = "other"  # type: ignore[misc]


def test_gds_export_result_normalizes_diagnostic_lists_to_tuples() -> None:
    errors = ["XSTRM error"]
    warnings = ["XSTRM warning"]
    result = GdsExportResult(
        status=ExecutionStatus.FAILURE,
        reason=GdsExportReason.XSTREAM_ERRORS,
        timed_out=False,
        library="demo",
        cell="top",
        view="layout",
        execution_time=1.0,
        errors=errors,  # type: ignore[arg-type]
        warnings=warnings,  # type: ignore[arg-type]
    )
    errors.append("later error")
    warnings.append("later warning")

    assert result.errors == ("XSTRM error",)
    assert result.warnings == ("XSTRM warning",)


@pytest.mark.parametrize(
    "status",
    [
        ExecutionStatus.SUCCESS,
        ExecutionStatus.FAILURE,
        ExecutionStatus.PARTIAL,
        ExecutionStatus.ERROR,
    ],
)
def test_gds_export_result_ok_only_for_success(status: ExecutionStatus) -> None:
    result = GdsExportResult(
        status=status,
        reason=GdsExportReason.COMPLETED,
        timed_out=False,
        library="demo",
        cell="top",
        view="layout",
        execution_time=0.1,
    )

    assert result.ok is (status == ExecutionStatus.SUCCESS)
    assert result.errors == ()
    assert result.warnings == ()


def test_validate_export_inputs_normalizes_paths_and_default_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    stream_map = _write_stream_map(home / "layers.map")
    monkeypatch.setenv("HOME", str(home))

    inputs = _validate_export_inputs(
        "demo",
        "top",
        "~/exports/../top.gds",
        stream_map="~/layers.map",
        view="layout",
        log_path=None,
        timeout=30,
        poll_interval=1,
        skill_timeout=5,
        finalization_reserve=2,
        cleanup_policy="success",
    )

    assert tuple(field.name for field in fields(type(inputs))) == _EXPORT_INPUT_FIELDS
    assert inputs.library == "demo"
    assert inputs.cell == "top"
    assert inputs.view == "layout"
    assert inputs.output_path == (home / "top.gds").resolve()
    assert inputs.log_path == (home / "top.xstream.log").resolve()
    assert inputs.stream_map == stream_map.resolve()
    assert inputs.timeout == 30.0
    assert inputs.poll_interval == 1.0
    assert inputs.skill_timeout == 5.0
    assert inputs.finalization_reserve == 2.0
    assert inputs.cleanup_policy == "success"
    with pytest.raises(FrozenInstanceError):
        inputs.cell = "other"  # type: ignore[misc]


def test_validate_export_inputs_normalizes_explicit_log_path(tmp_path: Path) -> None:
    stream_map = _write_stream_map(tmp_path / "tech" / "layers.map")
    kwargs = _validation_kwargs(stream_map)
    kwargs["log_path"] = tmp_path / "logs" / ".." / "custom.log"

    inputs = _validate_export_inputs(
        "demo",
        "top",
        tmp_path / "runs" / ".." / "top.gds",
        **kwargs,
    )

    assert inputs.output_path == (tmp_path / "top.gds").resolve()
    assert inputs.log_path == (tmp_path / "custom.log").resolve()


@pytest.mark.parametrize("map_kind", ["missing", "directory"])
def test_validate_export_inputs_requires_regular_stream_map(
    tmp_path: Path,
    map_kind: str,
) -> None:
    stream_map = tmp_path / "layers.map"
    if map_kind == "directory":
        stream_map.mkdir()

    with pytest.raises(FileNotFoundError):
        _validate_export_inputs(
            "demo",
            "top",
            tmp_path / "top.gds",
            **_validation_kwargs(stream_map),
        )


@pytest.mark.parametrize("alias_pair", ["output_log", "output_map", "log_map"])
def test_validate_export_inputs_rejects_all_normalized_path_aliases(
    tmp_path: Path,
    alias_pair: str,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.xstream.log"
    if alias_pair == "output_log":
        log_path = tmp_path / "nested" / ".." / "top.gds"
    elif alias_pair == "output_map":
        output_path = tmp_path / "nested" / ".." / "layers.map"
    else:
        log_path = tmp_path / "nested" / ".." / "layers.map"
    kwargs = _validation_kwargs(stream_map)
    kwargs["log_path"] = log_path

    with pytest.raises(ValueError, match="distinct"):
        _validate_export_inputs("demo", "top", output_path, **kwargs)


@pytest.mark.parametrize("alias_pair", ["output_map", "log_map", "output_log"])
def test_validate_export_inputs_rejects_existing_hardlink_aliases(
    tmp_path: Path,
    alias_pair: str,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.xstream.log"
    if alias_pair == "output_map":
        os.link(stream_map, output_path)
    elif alias_pair == "log_map":
        os.link(stream_map, log_path)
    else:
        output_path.write_bytes(b"synthetic gds")
        os.link(output_path, log_path)
    kwargs = _validation_kwargs(stream_map)
    kwargs["log_path"] = log_path

    with pytest.raises(ValueError, match="distinct"):
        _validate_export_inputs("demo", "top", output_path, **kwargs)


@pytest.mark.parametrize("field", ["library", "cell", "view"])
@pytest.mark.parametrize(
    "value",
    ["", "   ", "\t", None, 1],
    ids=["empty", "spaces", "tab", "none", "integer"],
)
def test_validate_export_inputs_requires_nonempty_string_names(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    names: dict[str, object] = {
        "library": "demo",
        "cell": "top",
        "view": "layout",
    }
    names[field] = value
    kwargs = _validation_kwargs(stream_map)
    kwargs["view"] = names["view"]

    with pytest.raises(ValueError, match=field):
        _validate_export_inputs(
            names["library"],  # type: ignore[arg-type]
            names["cell"],  # type: ignore[arg-type]
            tmp_path / "top.gds",
            **kwargs,
        )


def test_validate_export_inputs_preserves_nonempty_name_whitespace(
    tmp_path: Path,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    kwargs = _validation_kwargs(stream_map)
    kwargs["view"] = "layout "

    inputs = _validate_export_inputs(
        " demo",
        "\ttop",
        tmp_path / "top.gds",
        **kwargs,
    )

    assert inputs.library == " demo"
    assert inputs.cell == "\ttop"
    assert inputs.view == "layout "


@pytest.mark.parametrize(
    "field",
    ["timeout", "poll_interval", "skill_timeout", "finalization_reserve"],
)
@pytest.mark.parametrize(
    "value",
    [True, False, 0, -0.5, float("nan"), float("inf"), -float("inf"), "1", None],
    ids=[
        "true",
        "false",
        "zero",
        "negative",
        "nan",
        "positive_infinity",
        "negative_infinity",
        "string",
        "none",
    ],
)
def test_validate_export_inputs_rejects_invalid_numeric_controls(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    kwargs = _validation_kwargs(stream_map)
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        _validate_export_inputs(
            "demo",
            "top",
            tmp_path / "top.gds",
            **kwargs,
        )


@pytest.mark.parametrize("reserve", [30.0, 31.0])
def test_validate_export_inputs_requires_reserve_smaller_than_timeout(
    tmp_path: Path,
    reserve: float,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    kwargs = _validation_kwargs(stream_map)
    kwargs["finalization_reserve"] = reserve

    with pytest.raises(ValueError, match="finalization_reserve"):
        _validate_export_inputs(
            "demo",
            "top",
            tmp_path / "top.gds",
            **kwargs,
        )


@pytest.mark.parametrize("cleanup_policy", [None, "", "Success", "sometimes", True])
def test_validate_export_inputs_rejects_invalid_cleanup_policy(
    tmp_path: Path,
    cleanup_policy: object,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    kwargs = _validation_kwargs(stream_map)
    kwargs["cleanup_policy"] = cleanup_policy

    with pytest.raises(ValueError, match="cleanup_policy"):
        _validate_export_inputs(
            "demo",
            "top",
            tmp_path / "top.gds",
            **kwargs,
        )


@pytest.mark.parametrize("cleanup_policy", ["success", "always", "never"])
def test_validate_export_inputs_accepts_exact_cleanup_policies(
    tmp_path: Path,
    cleanup_policy: str,
) -> None:
    stream_map = _write_stream_map(tmp_path / "layers.map")
    kwargs = _validation_kwargs(stream_map)
    kwargs["cleanup_policy"] = cleanup_policy

    inputs = _validate_export_inputs(
        "demo",
        "top",
        tmp_path / "top.gds",
        **kwargs,
    )

    assert inputs.cleanup_policy == cleanup_policy


def test_budget_reserves_finalization_time_applies_cap_and_tracks_elapsed() -> None:
    clock = _FakeClock(100.0)
    budget = _Budget.start(10.0, 2.0, clock=clock)

    assert budget.started_at == 100.0
    assert budget.prefinalization_deadline == 108.0
    assert budget.deadline == 110.0
    assert budget.remaining(finalizing=False) == 8.0
    assert budget.remaining(finalizing=True) == 10.0
    assert budget.timeout(finalizing=False, cap=3.0) == 3.0
    assert budget.timeout(finalizing=False, cap=20.0) == 8.0
    assert budget.elapsed() == 0.0

    clock.now = 103.25
    assert budget.remaining(finalizing=False) == 4.75
    assert budget.remaining(finalizing=True) == 6.75
    assert budget.elapsed() == 3.25
    with pytest.raises(FrozenInstanceError):
        budget.deadline = 200.0  # type: ignore[misc]


def test_budget_timeout_preserves_positive_subsecond_remainder() -> None:
    clock = _FakeClock(0.0)
    budget = _Budget.start(0.75, 0.25, clock=clock)
    clock.now = 0.749999

    timeout = budget.timeout(finalizing=True)

    assert timeout == pytest.approx(0.000001)
    assert timeout > 0.0


def test_budget_prefinal_exhaustion_leaves_final_budget() -> None:
    clock = _FakeClock(20.0)
    budget = _Budget.start(5.0, 1.0, clock=clock)
    clock.now = 24.0

    assert budget.remaining(finalizing=False) == 0.0
    assert budget.remaining(finalizing=True) == 1.0
    with pytest.raises(_BudgetExpired):
        budget.timeout(finalizing=False)
    assert budget.timeout(finalizing=True) == 1.0


def test_budget_total_exhaustion_raises_for_timeout() -> None:
    clock = _FakeClock(20.0)
    budget = _Budget.start(5.0, 1.0, clock=clock)
    clock.now = 25.0

    assert budget.remaining(finalizing=False) == 0.0
    assert budget.remaining(finalizing=True) == 0.0
    with pytest.raises(_BudgetExpired):
        budget.timeout(finalizing=False)
    with pytest.raises(_BudgetExpired):
        budget.timeout(finalizing=True)


def test_budget_elapsed_never_returns_negative_for_regressing_clock() -> None:
    clock = _FakeClock(20.0)
    budget = _Budget.start(5.0, 1.0, clock=clock)
    clock.now = 19.5

    assert budget.elapsed() == 0.0


@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        ((), False),
        (("SKILL execution timeout in Virtuoso",), True),
        (("Socket timeout after 5s",), True),
        (("Socket timeout after 0.25s",), True),
        (("Socket timeout after 1e-06s",), True),
        (("Socket timeout after 2E+3s",), True),
        (
            (
                "SKILL execution timeout in Virtuoso",
                "Socket timeout after 12.5s",
            ),
            True,
        ),
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
        (
            (
                "Socket timeout after 5s",
                "other timeout while waiting",
            ),
            False,
        ),
    ],
)
def test_indeterminate_skill_timeout_requires_every_exact_match(
    errors: tuple[str, ...],
    expected: bool,
) -> None:
    assert _is_indeterminate_skill_timeout(errors) is expected


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        pytest.param(
            {
                "cleanup_failures": ("failed to restore logFile",),
                "log": _log_result(
                    completed=True,
                    error_count=None,
                    warning_count=None,
                    terminal_failures=("XSTRM-273: Translation failed",),
                ),
                "skill_errors": ("explicit SKILL failure",),
                "launch_indeterminate": True,
                "saw_evidence": False,
                "deadline_expired": True,
            },
            (
                ExecutionStatus.ERROR,
                GdsExportReason.REQUEST_CLEANUP_ERROR,
                True,
            ),
            id="cleanup-wins-over-all-overlapping-observations",
        ),
        pytest.param(
            {
                "log": _log_result(
                    completed=True,
                    error_count=None,
                    warning_count=0,
                    terminal_failures=("XSTRM-273: Translation failed",),
                ),
                "skill_errors": ("explicit SKILL failure",),
                "deadline_expired": True,
            },
            (ExecutionStatus.FAILURE, GdsExportReason.XSTREAM_FAILURE, True),
            id="terminal-log-wins-over-non-timeout-skill-error",
        ),
        pytest.param(
            {
                "log": _log_result(
                    completed=True,
                    error_count=None,
                    warning_count=0,
                    errors=("ERROR: diagnostic before malformed completion",),
                ),
                "skill_errors": ("explicit SKILL failure",),
                "deadline_expired": True,
            },
            (ExecutionStatus.ERROR, GdsExportReason.MALFORMED_LOG, True),
            id="malformed-error-count-wins-over-non-timeout-skill-error",
        ),
        pytest.param(
            {
                "log": _log_result(
                    completed=True,
                    error_count=0,
                    warning_count=None,
                ),
                "skill_errors": ("explicit SKILL failure",),
                "deadline_expired": False,
            },
            (ExecutionStatus.ERROR, GdsExportReason.MALFORMED_LOG, False),
            id="malformed-warning-count-wins-over-non-timeout-skill-error",
        ),
        pytest.param(
            {
                "log": _log_result(
                    completed=True,
                    error_count=2,
                    warning_count=0,
                    errors=("ERROR: diagnostic matching completion count",),
                ),
                "skill_errors": ("explicit SKILL failure",),
                "deadline_expired": True,
            },
            (ExecutionStatus.FAILURE, GdsExportReason.XSTREAM_ERRORS, True),
            id="xstream-error-count-wins-over-non-timeout-skill-error",
        ),
        pytest.param(
            {
                "log": _log_result(
                    completed=True,
                    error_count=0,
                    warning_count=0,
                ),
                "skill_errors": (
                    "Socket timeout after 1.5s",
                    "explicit SKILL failure",
                ),
                "gds_present": False,
                "deadline_expired": True,
            },
            (ExecutionStatus.ERROR, GdsExportReason.SKILL_ERROR, False),
            id="non-timeout-skill-error-wins-over-missing-gds",
        ),
        pytest.param(
            {
                "log": _log_result(
                    completed=True,
                    error_count=0,
                    warning_count=0,
                ),
                "gds_present": False,
                "deadline_expired": False,
            },
            (ExecutionStatus.PARTIAL, GdsExportReason.MISSING_GDS, True),
            id="valid-completion-with-missing-gds",
        ),
        pytest.param(
            {
                "log": _log_result(
                    completed=True,
                    error_count=0,
                    warning_count=1,
                ),
                "gds_present": True,
                "gds_size": 0,
            },
            (ExecutionStatus.PARTIAL, GdsExportReason.EMPTY_GDS, True),
            id="valid-completion-with-empty-gds",
        ),
        pytest.param(
            {
                "log": _log_result(),
                "launch_indeterminate": False,
                "saw_evidence": False,
            },
            (ExecutionStatus.PARTIAL, GdsExportReason.INCOMPLETE_LOG, True),
            id="returned-launch-with-incomplete-log",
        ),
        pytest.param(
            {
                "log": None,
                "launch_indeterminate": True,
                "saw_evidence": True,
            },
            (ExecutionStatus.PARTIAL, GdsExportReason.INCOMPLETE_LOG, True),
            id="indeterminate-launch-with-evidence-is-incomplete-log",
        ),
        pytest.param(
            {
                "log": None,
                "skill_errors": ("Socket timeout after 30s",),
                "launch_indeterminate": True,
                "saw_evidence": False,
            },
            (
                ExecutionStatus.PARTIAL,
                GdsExportReason.LAUNCH_INDETERMINATE,
                True,
            ),
            id="indeterminate-launch-without-evidence",
        ),
        pytest.param(
            {
                "log": _log_result(
                    completed=True,
                    error_count=0,
                    warning_count=2,
                    errors=("ERROR: retained zero-count diagnostic",),
                ),
                "gds_present": True,
                "gds_size": 1024,
                "gds_published": True,
                "deadline_expired": True,
            },
            (ExecutionStatus.SUCCESS, GdsExportReason.COMPLETED, False),
            id="valid-completion-with-published-nonempty-gds",
        ),
    ],
)
def test_classify_export_applies_exact_priority_matrix(
    observations: dict[str, object],
    expected: tuple[ExecutionStatus, GdsExportReason, bool],
) -> None:
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

    assert _classify_export(**defaults) == expected  # type: ignore[arg-type]


def test_classify_export_rejects_unclassifiable_publication_invariant() -> None:
    with pytest.raises(ValueError, match="unclassifiable"):
        _classify_export(
            cleanup_failures=(),
            log=_log_result(
                completed=True,
                error_count=0,
                warning_count=0,
            ),
            skill_errors=(),
            launch_indeterminate=False,
            saw_evidence=True,
            gds_present=True,
            gds_size=1,
            gds_published=False,
            deadline_expired=False,
        )


def test_export_gds_local_success_uses_direct_paths_without_transfers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(10.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "published" / "top.gds"
    log_path = tmp_path / "logs" / "top.log"
    stream_map = _write_stream_map(tmp_path / "maps" / "layers.map")
    client = _artifact_client()

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.timed_out is False
    assert result.library == "demo"
    assert result.cell == "top"
    assert result.view == "layout"
    assert result.execution_time == 0.0
    assert result.local_gds_path == output_path.resolve()
    assert result.local_log_path == log_path.resolve()
    assert result.local_run_dir is None
    assert result.remote_run_dir is None
    assert result.remote_files_retained is None
    assert result.errors == ()
    assert output_path.read_bytes() == b"current-run-gds"
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert len(client.skill_calls) == 1
    skill, used_timeout = client.skill_calls[0]
    run_dir, staged_gds, staged_log = _request_artifacts(skill)
    assert _skill_field(skill, "library") == "demo"
    assert _skill_field(skill, "topCell") == "top"
    assert _skill_field(skill, "view") == "layout"
    assert _skill_field(skill, "runDir") == str(run_dir)
    assert _skill_field(skill, "strmFile") == str(staged_gds)
    assert _skill_field(skill, "logFile") == str(staged_log)
    assert _skill_field(skill, "layerMap") == str(stream_map.resolve())
    assert run_dir.parent == output_path.parent.resolve()
    assert run_dir != output_path.parent.resolve()
    assert staged_gds == run_dir / "output.gds"
    assert staged_log == run_dir / "xstream.log"
    assert 0.0 < used_timeout <= 2.0
    assert clock.sleeps == []


def test_export_gds_ignores_is_remote_when_runner_is_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(is_remote=True)

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.local_gds_path == output_path.resolve()
    assert len(client.skill_calls) == 1


def test_export_gds_localhost_tunnel_shape_uses_local_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
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
    monkeypatch.setattr(client, "upload_file", forbidden)
    monkeypatch.setattr(client, "download_file", forbidden)
    monkeypatch.setattr(client, "dismiss_dialog", forbidden)

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.SUCCESS
    assert output_path.read_bytes() == b"current-run-gds"
    assert len(calls) == 1


def test_export_gds_non_none_runner_delegates_to_remote_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")

    class RemoteClient:
        ssh_runner = object()

        def execute_skill(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("remote dispatch must not enter local execution")

    client = RemoteClient()
    calls: list[tuple[object, object, object, object]] = []

    def remote_dispatch(
        used_client: object,
        inputs: object,
        budget: object,
        recovery_hook: object,
    ) -> GdsExportResult:
        calls.append((used_client, inputs, budget, recovery_hook))
        return GdsExportResult(
            status=ExecutionStatus.ERROR,
            reason=GdsExportReason.TRANSPORT_ERROR,
            timed_out=False,
            library="demo",
            cell="top",
            view="layout",
            execution_time=0.0,
            errors=("remote stub",),
        )

    monkeypatch.setattr(streamout, "_export_gds_remote", remote_dispatch)

    result = _run_export(client, output_path, stream_map)

    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.errors == ("remote stub",)
    assert len(calls) == 1
    assert calls[0][0] is client
    assert calls[0][3] is None


def test_export_gds_structures_ssh_runner_getter_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")

    class BrokenClient:
        @property
        def ssh_runner(self) -> object:
            raise RuntimeError("runner discovery failed")

    result = _run_export(BrokenClient(), output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.TRANSPORT_ERROR
    assert result.timed_out is False
    assert result.local_gds_path is None
    assert result.local_log_path is None
    assert result.local_run_dir is None
    assert any("runner discovery failed" in error for error in result.errors)


def test_export_gds_uses_unique_current_run_and_never_reports_stale_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"stale-old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    response = VirtuosoResult(
        status=ExecutionStatus.ERROR,
        errors=["explicit SKILL failure"],
    )
    first = _artifact_client(log_text=None, gds=None, response=response)
    second = _artifact_client(log_text=None, gds=None, response=response)

    first_result = _run_export(
        first,
        output_path,
        stream_map,
        cleanup_policy="never",
    )
    second_result = _run_export(
        second,
        output_path,
        stream_map,
        cleanup_policy="never",
    )

    assert first_result.reason == GdsExportReason.SKILL_ERROR
    assert second_result.reason == GdsExportReason.SKILL_ERROR
    assert first_result.local_gds_path is None
    assert second_result.local_gds_path is None
    assert first_result.local_run_dir is not None
    assert second_result.local_run_dir is not None
    assert first_result.local_run_dir != second_result.local_run_dir
    assert first_result.local_run_dir.parent == output_path.parent.resolve()
    assert second_result.local_run_dir.parent == output_path.parent.resolve()
    assert output_path.read_bytes() == b"stale-old-gds"


def test_export_gds_polls_generic_error_until_zero_count_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged: dict[str, Path] = {}

    def execute(skill: str, _timeout: float) -> VirtuosoResult:
        run_dir, gds_path, log_path = _write_artifacts(
            skill,
            log_text=_PARTIAL_WITH_ERROR_LOG,
            gds=None,
        )
        staged.update(run_dir=run_dir, gds=gds_path, log=log_path)
        return _started_result()

    def finish(_seconds: float) -> None:
        staged["log"].write_text(
            _ZERO_COUNT_WITH_ERROR_LOG,
            encoding="utf-8",
        )
        staged["gds"].write_bytes(b"finished-after-one-poll")

    clock = _FakeClock(0.0, on_sleep=finish)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _FakeLocalClient(execute)

    result = _run_export(client, output_path, stream_map, poll_interval=0.25)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.log_result is not None
    assert result.log_result.errors == (
        "ERROR: current-run XStream error despite zero completion count",
    )
    assert result.errors == result.log_result.errors
    assert output_path.read_bytes() == b"finished-after-one-poll"
    assert clock.sleeps == [0.25]


def test_export_gds_observes_artifacts_when_skill_returns_at_prefinal_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")

    def execute(skill: str, _timeout: float) -> object:
        _write_artifacts(skill)
        clock.now = 3.0
        return {
            "status": "error",
            "errors": ["SKILL execution timeout in Virtuoso"],
        }

    client = _FakeLocalClient(execute)

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.local_gds_path == output_path.resolve()
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert result.local_log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert clock.sleeps == []


def test_export_gds_reobserves_artifacts_completed_during_final_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged: dict[str, Path] = {}

    def execute(skill: str, _timeout: float) -> VirtuosoResult:
        _run_dir, gds_path, log_path = _write_artifacts(
            skill,
            log_text=_PARTIAL_LOG,
            gds=None,
        )
        staged.update(gds=gds_path, log=log_path)
        return _started_result()

    clock = _FakeClock(0.0)

    def complete_on_boundary(_seconds: float) -> None:
        if clock.now == pytest.approx(1.5):
            staged["log"].write_text(_SUCCESS_LOG, encoding="utf-8")
            staged["gds"].write_bytes(b"boundary-gds")

    clock.on_sleep = complete_on_boundary
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _FakeLocalClient(execute)

    result = _run_export(
        client,
        output_path,
        stream_map,
        timeout=2.0,
        finalization_reserve=0.5,
        poll_interval=1.0,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert output_path.read_bytes() == b"boundary-gds"
    assert clock.sleeps == [1.0, 0.5]


def test_export_gds_does_not_observe_after_sleep_overshoots_total_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged: dict[str, Path] = {}

    def execute(skill: str, _timeout: float) -> VirtuosoResult:
        _run_dir, gds_path, log_path = _write_artifacts(
            skill,
            log_text=_PARTIAL_LOG,
            gds=None,
        )
        staged.update(gds=gds_path, log=log_path)
        return _started_result()

    def oversleep_with_completion(_seconds: float) -> None:
        clock.now = 5.0
        staged["log"].write_text(_SUCCESS_LOG, encoding="utf-8")
        staged["gds"].write_bytes(b"completed-during-oversleep")

    clock = _FakeClock(0.0, on_sleep=oversleep_with_completion)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _FakeLocalClient(execute)
    real_observe = streamout._observe_local_artifacts
    observation_calls: list[Path] = []

    def record_observation(paths: Any) -> Any:
        observation_calls.append(paths.run_dir)
        return real_observe(paths)

    monkeypatch.setattr(
        streamout,
        "_observe_local_artifacts",
        record_observation,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.timed_out is True
    assert result.log_result is not None
    assert result.log_result.completed is False
    assert result.local_gds_path is None
    assert result.local_log_path is None
    assert result.local_run_dir is not None
    assert (result.local_run_dir / "output.gds").read_bytes() == (
        b"completed-during-oversleep"
    )
    assert len(observation_calls) == 1
    assert clock.sleeps == [0.5]


def test_export_gds_structures_sleep_overflow_for_large_finite_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(log_text=None, gds=None)
    sleep_calls: list[float] = []

    def overflow(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise OverflowError("timestamp out of range")

    monkeypatch.setattr(streamout, "_SLEEP", overflow)

    result = _run_export(
        client,
        output_path,
        stream_map,
        timeout=1e308,
        poll_interval=1e308,
        skill_timeout=1.0,
        finalization_reserve=1.0,
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.STAGING_ERROR
    assert result.timed_out is False
    assert sleep_calls == [pytest.approx(1e308)]
    assert any("timestamp out of range" in error for error in result.errors)


@pytest.mark.parametrize(
    ("log_text", "expected_status", "expected_reason"),
    [
        (
            _TERMINAL_LOG,
            ExecutionStatus.FAILURE,
            GdsExportReason.XSTREAM_FAILURE,
        ),
        (
            _MALFORMED_LOG,
            ExecutionStatus.ERROR,
            GdsExportReason.MALFORMED_LOG,
        ),
        (
            _ERROR_COMPLETION_LOG,
            ExecutionStatus.FAILURE,
            GdsExportReason.XSTREAM_ERRORS,
        ),
    ],
)
def test_export_gds_terminal_and_invalid_completions_exit_without_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_text: str,
    expected_status: ExecutionStatus,
    expected_reason: GdsExportReason,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(log_text=log_text)

    result = _run_export(client, output_path, stream_map)

    assert result.status == expected_status
    assert result.reason == expected_reason
    assert result.timed_out is False
    assert result.local_gds_path is None
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert output_path.read_bytes() == b"old-gds"
    assert clock.sleeps == []


def test_export_gds_exact_skill_timeout_can_recover_from_current_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    timeout_error = "SKILL execution timeout in Virtuoso"
    response = {
        "status": ExecutionStatus.ERROR,
        "errors": [timeout_error],
    }
    client = _artifact_client(response=response)
    recovery_calls: list[str] = []

    result = _run_export(
        client,
        output_path,
        stream_map,
        recovery_hook=lambda: recovery_calls.append("called"),
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.timed_out is False
    assert result.errors == (timeout_error,)
    assert recovery_calls == ["called"]
    assert clock.sleeps == []


def test_export_gds_timeout_without_progress_waits_then_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    response = {
        "status": "error",
        "errors": ["Socket timeout after 1.5s"],
    }
    client = _artifact_client(log_text=None, gds=None, response=response)
    recovery_calls: list[str] = []

    result = _run_export(
        client,
        output_path,
        stream_map,
        timeout=3.0,
        finalization_reserve=1.0,
        poll_interval=0.75,
        recovery_hook=lambda: recovery_calls.append("called"),
    )

    assert result.status == ExecutionStatus.PARTIAL
    assert result.reason == GdsExportReason.LAUNCH_INDETERMINATE
    assert result.timed_out is True
    assert result.local_gds_path is None
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert "Socket timeout after 1.5s" in result.local_log_path.read_text(
        encoding="utf-8"
    )
    assert recovery_calls == []
    assert clock.sleeps == [0.75, 0.75, 0.5]
    assert sum(clock.sleeps) == 2.0


def test_export_gds_recovery_hook_runs_once_after_progress_and_warning_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged: dict[str, Path] = {}

    def execute(skill: str, _timeout: float) -> object:
        _run_dir, gds_path, log_path = _write_artifacts(
            skill,
            log_text=_PARTIAL_LOG,
            gds=None,
        )
        staged.update(gds=gds_path, log=log_path)
        return {
            "status": "error",
            "errors": ["SKILL execution timeout in Virtuoso"],
        }

    def finish(_seconds: float) -> None:
        if len(clock.sleeps) == 1:
            staged["log"].write_text(
                _PARTIAL_LOG + "WARNING: recovery still in progress\n",
                encoding="utf-8",
            )
        else:
            staged["log"].write_text(_SUCCESS_LOG, encoding="utf-8")
            staged["gds"].write_bytes(b"recovered")

    hook_calls: list[str] = []

    def recovery_hook() -> None:
        hook_calls.append("called")
        raise RuntimeError("X11 recovery unavailable")

    clock = _FakeClock(0.0, on_sleep=finish)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _FakeLocalClient(execute)

    result = _run_export(
        client,
        output_path,
        stream_map,
        recovery_hook=recovery_hook,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert output_path.read_bytes() == b"recovered"
    assert hook_calls == ["called"]
    assert len(result.warnings) == 1
    assert "X11 recovery unavailable" in result.warnings[0]
    assert clock.sleeps == [0.5, 0.5]


def test_export_gds_normal_response_never_calls_recovery_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()

    def forbidden_hook() -> None:
        raise AssertionError("normal launch must not recover")

    result = _run_export(
        client,
        output_path,
        stream_map,
        recovery_hook=forbidden_hook,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.warnings == ()


def test_export_gds_default_recovery_is_headless_for_indeterminate_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(
        log_text=_PARTIAL_LOG,
        gds=None,
        response=VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["SKILL execution timeout in Virtuoso"],
        ),
    )

    result = streamout.export_gds(
        client,
        "demo",
        "top",
        output_path,
        stream_map=stream_map,
        timeout=2.0,
        poll_interval=0.5,
        skill_timeout=1.0,
        finalization_reserve=0.5,
    )

    assert result.status == ExecutionStatus.PARTIAL
    assert result.reason == GdsExportReason.INCOMPLETE_LOG
    assert result.timed_out is True
    assert clock.sleeps == [0.5, 0.5, 0.5]


@pytest.mark.parametrize(
    ("response", "diagnostic"),
    [
        (
            VirtuosoResult(
                status=ExecutionStatus.ERROR,
                errors=["explicit SKILL failure"],
            ),
            "explicit SKILL failure",
        ),
        (
            {"status": "error", "errors": []},
            "SKILL request returned non-success status",
        ),
        (
            {
                "status": "success",
                "output": '("xstreamRequest" "failed" "body failed" nil)',
            },
            "body failed",
        ),
        (
            {"status": "success", "output": "malformed wire"},
            "malformed XStream request response",
        ),
    ],
    ids=["explicit", "status-only", "body", "malformed-wire"],
)
def test_export_gds_non_timeout_skill_failures_observe_once_without_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: object,
    diagnostic: str,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(
        log_text=_PARTIAL_LOG + "INFO: launch diagnostic\n",
        gds=b"unvalidated",
        response=response,
    )
    observation_calls: list[Path] = []
    real_observe = streamout._observe_local_artifacts

    def recording_observe(paths: Any) -> Any:
        observation_calls.append(paths.run_dir)
        return real_observe(paths)

    monkeypatch.setattr(
        streamout,
        "_observe_local_artifacts",
        recording_observe,
    )

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.SKILL_ERROR
    assert result.timed_out is False
    assert any(diagnostic in error for error in result.errors)
    assert result.local_gds_path is None
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert "launch diagnostic" in result.local_log_path.read_text(encoding="utf-8")
    assert output_path.read_bytes() == b"old-gds"
    assert len(observation_calls) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("log_text", "expected_status", "expected_reason"),
    [
        (
            _TERMINAL_LOG,
            ExecutionStatus.FAILURE,
            GdsExportReason.XSTREAM_FAILURE,
        ),
        (
            _MALFORMED_LOG,
            ExecutionStatus.ERROR,
            GdsExportReason.MALFORMED_LOG,
        ),
        (
            _ERROR_COMPLETION_LOG,
            ExecutionStatus.FAILURE,
            GdsExportReason.XSTREAM_ERRORS,
        ),
    ],
    ids=["terminal", "malformed", "nonzero"],
)
def test_export_gds_non_timeout_skill_error_yields_to_higher_priority_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_text: str,
    expected_status: ExecutionStatus,
    expected_reason: GdsExportReason,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(
        log_text=log_text,
        gds=b"diagnostic-only-gds",
        response=VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["explicit SKILL failure"],
        ),
    )

    result = _run_export(client, output_path, stream_map)

    assert result.status == expected_status
    assert result.reason == expected_reason
    assert result.timed_out is False
    assert "explicit SKILL failure" in result.errors
    assert result.local_gds_path is None
    assert output_path.read_bytes() == b"old-gds"
    assert clock.sleeps == []


def test_export_gds_refreshes_skill_error_log_before_final_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(
        log_text=_PARTIAL_LOG,
        gds=None,
        response=VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["explicit SKILL failure"],
        ),
    )
    real_finalize = streamout._finalize_local_export

    def append_terminal_before_finalization(
        inputs: object,
        paths: Any,
        budget: object,
        outcome: object,
        **kwargs: Any,
    ) -> GdsExportResult:
        paths.log.write_text(_TERMINAL_LOG, encoding="utf-8")
        return real_finalize(inputs, paths, budget, outcome, **kwargs)

    monkeypatch.setattr(
        streamout,
        "_finalize_local_export",
        append_terminal_before_finalization,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.timed_out is False
    assert "explicit SKILL failure" in result.errors
    assert result.log_result is not None
    assert result.log_result.terminal_failures
    assert result.local_gds_path is None
    assert result.local_log_path == log_path.resolve()
    assert log_path.read_text(encoding="utf-8") == _TERMINAL_LOG
    assert output_path.read_bytes() == b"old-gds"
    assert clock.sleeps == []


def test_export_gds_structures_execute_skill_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")

    def execute(_skill: str, _timeout: float) -> object:
        raise RuntimeError("execute transport broke")

    result = _run_export(_FakeLocalClient(execute), output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.SKILL_ERROR
    assert any("execute transport broke" in error for error in result.errors)
    assert result.local_log_path == output_path.with_name("top.xstream.log")


def test_export_gds_structures_response_normalization_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(log_text=None, gds=None)

    def fail_normalization(_response: object) -> object:
        raise RuntimeError("normalization broke")

    monkeypatch.setattr(streamout, "response_fields", fail_normalization)

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.SKILL_ERROR
    assert any("normalization broke" in error for error in result.errors)
    assert result.local_log_path == output_path.with_name("top.xstream.log")


def test_export_gds_preserves_virtuoso_result_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(
        response=VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output=_STARTED_WIRE,
            warnings=["bridge warning"],
        )
    )

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.warnings == ("bridge warning",)


def test_export_gds_structures_surrogate_diagnostic_encoding_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(
        log_text=None,
        gds=None,
        response=VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["explicit SKILL failure"],
        ),
    )

    result = streamout.export_gds(
        client,
        "demo\udcff",
        "top",
        output_path,
        stream_map=stream_map,
        timeout=4.0,
        poll_interval=0.5,
        skill_timeout=2.0,
        finalization_reserve=1.0,
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.local_log_path is None
    assert any("surrogate" in error.lower() for error in result.errors)


def test_export_gds_request_cleanup_failure_beats_successful_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    response = {
        "status": "success",
        "output": (
            '("xstreamRequest" "started" nil '
            '("failed to restore XStream field logFile"))'
        ),
    }
    client = _artifact_client(response=response)

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.REQUEST_CLEANUP_ERROR
    assert result.timed_out is False
    assert result.errors[0] == "failed to restore XStream field logFile"
    assert result.local_gds_path is None
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert output_path.read_bytes() == b"old-gds"
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("gds", "expected_reason"),
    [
        (None, GdsExportReason.MISSING_GDS),
        (b"", GdsExportReason.EMPTY_GDS),
    ],
    ids=["missing", "empty"],
)
def test_export_gds_valid_completion_waits_for_missing_or_empty_gds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gds: bytes | None,
    expected_reason: GdsExportReason,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(gds=gds)

    result = _run_export(
        client,
        output_path,
        stream_map,
        timeout=2.0,
        finalization_reserve=0.5,
        poll_interval=1.0,
    )

    assert result.status == ExecutionStatus.PARTIAL
    assert result.reason == expected_reason
    assert result.timed_out is True
    assert result.local_gds_path is None
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert output_path.read_bytes() == b"old-gds"
    assert clock.sleeps == [1.0, 0.5]
    assert result.local_run_dir is not None
    assert not (result.local_run_dir / "output.gds").exists()


def test_export_gds_incomplete_log_waits_to_prefinal_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(log_text=_PARTIAL_LOG, gds=None)

    result = _run_export(
        client,
        output_path,
        stream_map,
        timeout=2.0,
        finalization_reserve=0.5,
        poll_interval=0.6,
    )

    assert result.status == ExecutionStatus.PARTIAL
    assert result.reason == GdsExportReason.INCOMPLETE_LOG
    assert result.timed_out is True
    assert result.log_result is not None
    assert result.log_result.completed is False
    assert clock.sleeps == [0.6, 0.6, pytest.approx(0.3)]


def test_publish_file_uses_temporary_sibling_and_cleans_it_on_replace_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.gds"
    destination = tmp_path / "published" / ("d" * 200 + ".gds")
    source.write_bytes(b"new-gds")
    destination.parent.mkdir()
    destination.write_bytes(b"old-gds")
    replace_calls: list[tuple[Path, Path]] = []

    def fail_replace(source_path: str | Path, destination_path: str | Path) -> None:
        replace_calls.append((Path(source_path), Path(destination_path)))
        raise OSError("replace denied")

    monkeypatch.setattr(streamout.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace denied"):
        streamout._publish_file(source, destination)

    assert len(replace_calls) == 1
    temporary, used_destination = replace_calls[0]
    assert temporary.parent == destination.parent
    assert temporary != destination
    assert temporary.name.startswith(".vbp-")
    assert len(temporary.name) <= 48
    assert destination.name not in temporary.name
    assert used_destination == destination
    assert not temporary.exists()
    assert destination.read_bytes() == b"old-gds"


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode semantics")
@pytest.mark.parametrize("suffix", [".log", ".gds"])
def test_publish_file_preserves_existing_destination_mode(
    tmp_path: Path,
    suffix: str,
) -> None:
    source = tmp_path / f"source{suffix}"
    destination = tmp_path / f"published{suffix}"
    source.write_bytes(b"new-data")
    destination.write_bytes(b"old-data")
    source.chmod(0o664)
    destination.chmod(0o600)
    original_owner = destination.stat().st_uid

    streamout._publish_file(source, destination)

    metadata = destination.stat()
    assert destination.read_bytes() == b"new-data"
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == original_owner


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode semantics")
@pytest.mark.parametrize("suffix", [".log", ".gds"])
def test_publish_file_first_publication_inherits_source_mode(
    tmp_path: Path,
    suffix: str,
) -> None:
    source = tmp_path / f"source{suffix}"
    destination = tmp_path / f"published{suffix}"
    source.write_bytes(b"new-data")
    source.chmod(0o640)

    streamout._publish_file(source, destination)

    assert destination.read_bytes() == b"new-data"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner semantics")
def test_publish_file_refuses_to_change_existing_destination_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.log"
    destination = tmp_path / "published.log"
    source.write_bytes(b"new-data")
    destination.write_bytes(b"old-data")
    real_stat = Path.stat

    def report_different_temp_owner(path: Path, *args: Any, **kwargs: Any) -> Any:
        metadata = real_stat(path, *args, **kwargs)
        if path.parent == tmp_path and path not in (source, destination):
            values = list(metadata)
            values[4] = metadata.st_uid + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "stat", report_different_temp_owner)

    with pytest.raises(OSError, match="owner"):
        streamout._publish_file(source, destination)

    assert destination.read_bytes() == b"old-data"
    assert not tuple(tmp_path.glob(".vbp-*.tmp"))


def test_publish_file_rejects_empty_temporary_before_replace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty.gds"
    destination = tmp_path / "top.gds"
    source.write_bytes(b"")
    destination.write_bytes(b"old-gds")

    with pytest.raises(OSError, match="empty"):
        streamout._publish_file(source, destination)

    assert destination.read_bytes() == b"old-gds"
    assert not tuple(tmp_path.glob(".vbp-*.tmp"))


def test_publish_file_rejects_source_change_during_validator(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.gds"
    destination = tmp_path / "published.gds"
    source.write_bytes(b"copied-snapshot")
    destination.write_bytes(b"old-gds")

    def mutate_source() -> None:
        source.write_bytes(b"newer-source")

    with pytest.raises(streamout._SourceSnapshotChanged):
        streamout._publish_file(
            source,
            destination,
            validator=mutate_source,
        )

    assert source.read_bytes() == b"newer-source"
    assert destination.read_bytes() == b"old-gds"
    assert not tuple(tmp_path.glob(".vbp-*.tmp"))


def test_export_gds_publishes_log_before_gds_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()
    publish_order: list[Path] = []
    real_publish = streamout._publish_file

    def recording_publish(
        source: Path,
        destination: Path,
        *,
        validator: Callable[[], None] | None = None,
    ) -> None:
        publish_order.append(destination)
        if validator is None:
            real_publish(source, destination)
        else:
            real_publish(source, destination, validator=validator)

    monkeypatch.setattr(streamout, "_publish_file", recording_publish)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert publish_order == [log_path.resolve(), output_path.resolve()]


def test_export_gds_revalidates_gds_after_log_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()
    real_publish = streamout._publish_file

    def truncate_after_log(source: Path, destination: Path) -> None:
        real_publish(source, destination)
        if destination == log_path.resolve():
            (source.parent / "output.gds").write_bytes(b"")

    monkeypatch.setattr(streamout, "_publish_file", truncate_after_log)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.PARTIAL
    assert result.reason == GdsExportReason.EMPTY_GDS
    assert result.timed_out is True
    assert result.local_gds_path is None
    assert result.local_log_path == log_path.resolve()
    assert result.local_run_dir is not None
    assert output_path.read_bytes() == b"old-gds"


def test_export_gds_rechecks_log_after_log_publication_before_gds_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(gds=b"must-not-publish")
    real_publish = streamout._publish_file
    changed_live_log = False

    def append_terminal_after_log_publish(
        source: Path,
        destination: Path,
    ) -> None:
        nonlocal changed_live_log
        real_publish(source, destination)
        if destination == log_path.resolve() and not changed_live_log:
            changed_live_log = True
            (source.parent / "xstream.log").write_text(
                _TERMINAL_LOG,
                encoding="utf-8",
            )

    monkeypatch.setattr(
        streamout,
        "_publish_file",
        append_terminal_after_log_publish,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.local_gds_path is None
    assert result.local_log_path == log_path.resolve()
    assert result.log_result is not None
    assert result.log_result.terminal_failures
    assert result.errors == result.log_result.terminal_failures
    assert log_path.read_text(encoding="utf-8") == _TERMINAL_LOG
    assert output_path.read_bytes() == b"old-gds"


def test_export_gds_rechecks_log_after_gds_revalidation_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(gds=b"must-not-publish")
    real_publish = streamout._publish_file
    real_file_size = streamout._local_file_size
    log_published = False
    changed_live_log = False

    def record_log_publication(source: Path, destination: Path) -> None:
        nonlocal log_published
        real_publish(source, destination)
        if destination == log_path.resolve():
            log_published = True

    def append_terminal_during_gds_revalidation(path: Path) -> tuple[bool, int]:
        nonlocal changed_live_log
        result = real_file_size(path)
        if log_published and path.name == "output.gds" and not changed_live_log:
            changed_live_log = True
            path.with_name("xstream.log").write_text(
                _TERMINAL_LOG,
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(streamout, "_publish_file", record_log_publication)
    monkeypatch.setattr(
        streamout,
        "_local_file_size",
        append_terminal_during_gds_revalidation,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.local_gds_path is None
    assert result.local_log_path == log_path.resolve()
    assert result.log_result is not None
    assert result.log_result.terminal_failures
    assert result.errors == result.log_result.terminal_failures
    assert log_path.read_text(encoding="utf-8") == _TERMINAL_LOG
    assert output_path.read_bytes() == b"old-gds"


def test_export_gds_validates_log_inside_gds_publication_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(gds=b"must-not-publish")
    real_publish = streamout._publish_file
    validator_calls = 0

    def mutate_before_replace(
        source: Path,
        destination: Path,
        *,
        validator: Callable[[], None] | None = None,
    ) -> None:
        nonlocal validator_calls
        if destination != output_path.resolve():
            real_publish(source, destination)
            return
        assert validator is not None

        def append_terminal_then_validate() -> None:
            nonlocal validator_calls
            validator_calls += 1
            temporary_files = tuple(
                destination.parent.glob(".vbp-*.tmp")
            )
            assert len(temporary_files) == 1
            assert temporary_files[0].read_bytes() == source.read_bytes()
            source.with_name("xstream.log").write_text(
                _TERMINAL_LOG,
                encoding="utf-8",
            )
            validator()

        real_publish(
            source,
            destination,
            validator=append_terminal_then_validate,
        )

    monkeypatch.setattr(streamout, "_publish_file", mutate_before_replace)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.local_gds_path is None
    assert result.local_log_path == log_path.resolve()
    assert result.log_result is not None
    assert result.log_result.terminal_failures
    assert log_path.read_text(encoding="utf-8") == _TERMINAL_LOG
    assert output_path.read_bytes() == b"old-gds"
    assert validator_calls == 1
    assert not tuple(tmp_path.glob(".vbp-*.tmp"))


def test_export_gds_bounds_pre_replace_log_change_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(gds=b"must-not-publish")
    real_publish = streamout._publish_file
    validator_calls = 0

    def keep_changing_before_replace(
        source: Path,
        destination: Path,
        *,
        validator: Callable[[], None] | None = None,
    ) -> None:
        nonlocal validator_calls
        if destination != output_path.resolve():
            real_publish(source, destination)
            return
        assert validator is not None

        def change_valid_log_then_validate() -> None:
            nonlocal validator_calls
            validator_calls += 1
            source.with_name("xstream.log").write_text(
                _SUCCESS_LOG + f"INFO: late update {validator_calls}\n",
                encoding="utf-8",
            )
            validator()

        real_publish(
            source,
            destination,
            validator=change_valid_log_then_validate,
        )

    monkeypatch.setattr(
        streamout,
        "_publish_file",
        keep_changing_before_replace,
    )

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.local_gds_path is None
    assert output_path.read_bytes() == b"old-gds"
    assert validator_calls == streamout._MAX_FINAL_LOG_REFRESHES
    assert any("did not stabilize" in error for error in result.errors)
    assert not tuple(tmp_path.glob(".vbp-*.tmp"))


def test_export_gds_retries_same_size_source_rewrite_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    published_old = b"published-old"
    output_path.write_bytes(published_old)
    stream_map = _write_stream_map(tmp_path / "layers.map")
    original_gds = b"A" * 16
    replacement_gds = b"B" * len(original_gds)
    client = _artifact_client(gds=original_gds)
    real_copyfileobj = streamout.shutil.copyfileobj
    gds_copy_attempts = 0
    copied_attempts: list[bytes] = []

    def rewrite_during_first_gds_copy(
        source_file: Any,
        destination_file: Any,
        length: int = 0,
    ) -> None:
        nonlocal gds_copy_attempts
        source_path = Path(source_file.name)
        if source_path.name != "output.gds":
            real_copyfileobj(source_file, destination_file, length)
            return

        gds_copy_attempts += 1
        if gds_copy_attempts == 2:
            assert output_path.read_bytes() == published_old
        if gds_copy_attempts == 1:
            midpoint = len(original_gds) // 2
            destination_file.write(source_file.read(midpoint))
            source_metadata = os.fstat(source_file.fileno())
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

    monkeypatch.setattr(
        streamout.shutil,
        "copyfileobj",
        rewrite_during_first_gds_copy,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.local_gds_path == output_path.resolve()
    assert result.errors == ()
    assert gds_copy_attempts == 2
    assert copied_attempts == [b"A" * 8 + b"B" * 8, replacement_gds]
    assert output_path.read_bytes() == replacement_gds
    assert result.local_run_dir is not None
    assert (result.local_run_dir / "output.gds").read_bytes() == replacement_gds
    assert not tuple(tmp_path.rglob(".vbp-*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX open-file replacement")
def test_export_gds_retries_source_path_replacement_after_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    published_old = b"published-old"
    output_path.write_bytes(published_old)
    stream_map = _write_stream_map(tmp_path / "layers.map")
    original_gds = b"A" * 16
    replacement_gds = b"C" * len(original_gds)
    client = _artifact_client(gds=original_gds)
    real_copyfileobj = streamout.shutil.copyfileobj
    gds_copy_attempts = 0
    copied_attempts: list[bytes] = []

    def replace_path_after_first_gds_copy(
        source_file: Any,
        destination_file: Any,
        length: int = 0,
    ) -> None:
        nonlocal gds_copy_attempts
        source_path = Path(source_file.name)
        if source_path.name != "output.gds":
            real_copyfileobj(source_file, destination_file, length)
            return

        gds_copy_attempts += 1
        if gds_copy_attempts == 2:
            assert output_path.read_bytes() == published_old
        real_copyfileobj(source_file, destination_file, length)
        destination_file.flush()
        copied_attempts.append(Path(destination_file.name).read_bytes())
        if gds_copy_attempts == 1:
            source_metadata = os.fstat(source_file.fileno())
            replacement_path = source_path.with_name("replacement.gds")
            replacement_path.write_bytes(replacement_gds)
            os.replace(replacement_path, source_path)
            assert not os.path.samestat(source_metadata, source_path.stat())

    monkeypatch.setattr(
        streamout.shutil,
        "copyfileobj",
        replace_path_after_first_gds_copy,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.local_gds_path == output_path.resolve()
    assert result.errors == ()
    assert gds_copy_attempts == 2
    assert copied_attempts == [original_gds, replacement_gds]
    assert output_path.read_bytes() == replacement_gds
    assert result.local_run_dir is not None
    assert (result.local_run_dir / "output.gds").read_bytes() == replacement_gds
    assert not tuple(tmp_path.rglob(".vbp-*.tmp"))


def test_export_gds_uses_read_snapshot_size_for_parse_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(log_text="", gds=b"snapshot-gds")
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    completed_before_read = False

    def materialize_completion(path: Path) -> None:
        nonlocal completed_before_read
        if path.name == "xstream.log" and not completed_before_read:
            completed_before_read = True
            path.write_bytes(_SUCCESS_LOG.encode("utf-8"))

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        materialize_completion(path)
        return real_read_text(path, *args, **kwargs)

    def read_bytes(path: Path) -> bytes:
        materialize_completion(path)
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.errors == ()
    assert result.local_log_path == log_path.resolve()
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert output_path.read_bytes() == b"snapshot-gds"


def test_export_gds_rechecks_final_log_snapshot_before_gds_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(gds=b"must-not-publish")
    real_finalize = streamout._finalize_local_export

    def append_terminal_before_finalization(
        inputs: object,
        paths: Any,
        budget: object,
        outcome: object,
        **kwargs: Any,
    ) -> GdsExportResult:
        paths.log.write_text(_TERMINAL_LOG, encoding="utf-8")
        return real_finalize(inputs, paths, budget, outcome, **kwargs)

    monkeypatch.setattr(
        streamout,
        "_finalize_local_export",
        append_terminal_before_finalization,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.local_gds_path is None
    assert result.local_log_path == log_path.resolve()
    assert result.log_result is not None
    assert result.log_result.terminal_failures
    assert log_path.read_text(encoding="utf-8") == _TERMINAL_LOG
    assert output_path.read_bytes() == b"old-gds"


def test_export_gds_zero_completion_count_with_error_line_publishes_gds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(
        log_text=_ZERO_COUNT_WITH_ERROR_LOG,
        gds=b"current-run-gds-with-diagnostic",
    )

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.local_gds_path == output_path.resolve()
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert result.log_result is not None
    assert result.log_result.errors == (
        "ERROR: current-run XStream error despite zero completion count",
    )
    assert result.errors == result.log_result.errors
    assert output_path.read_bytes() == b"current-run-gds-with-diagnostic"


def test_export_gds_log_publication_crossing_deadline_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(log_text=_TERMINAL_LOG, gds=None)
    real_publish = streamout._publish_file

    def consume_deadline(source: Path, destination: Path) -> None:
        real_publish(source, destination)
        if destination == log_path.resolve():
            clock.now = 4.0

    monkeypatch.setattr(streamout, "_publish_file", consume_deadline)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
        cleanup_policy="never",
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.timed_out is True
    assert result.local_log_path == log_path.resolve()
    assert result.local_gds_path is None
    assert log_path.read_text(encoding="utf-8") == _TERMINAL_LOG


def test_export_gds_log_publication_failure_blocks_gds_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    log_path.write_text("old-log\n", encoding="utf-8")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()
    publish_order: list[Path] = []

    def fail_log(_source: Path, destination: Path) -> None:
        publish_order.append(destination)
        raise OSError("log publication denied")

    monkeypatch.setattr(streamout, "_publish_file", fail_log)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.timed_out is False
    assert result.local_log_path is None
    assert result.local_gds_path is None
    assert publish_order == [log_path.resolve()]
    assert output_path.read_bytes() == b"old-gds"
    assert log_path.read_text(encoding="utf-8") == "old-log\n"


def test_export_gds_gds_publication_failure_preserves_old_gds_and_new_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    log_path = tmp_path / "top.log"
    output_path.write_bytes(b"old-gds")
    log_path.write_text("old-log\n", encoding="utf-8")
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()
    publish_order: list[Path] = []
    real_publish = streamout._publish_file

    def fail_gds(
        source: Path,
        destination: Path,
        *,
        validator: Callable[[], None] | None = None,
    ) -> None:
        publish_order.append(destination)
        if destination == output_path.resolve():
            raise OSError("GDS publication denied")
        if validator is None:
            real_publish(source, destination)
        else:
            real_publish(source, destination, validator=validator)

    monkeypatch.setattr(streamout, "_publish_file", fail_gds)

    result = _run_export(
        client,
        output_path,
        stream_map,
        log_path=log_path,
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.local_log_path == log_path.resolve()
    assert result.local_gds_path is None
    assert publish_order == [log_path.resolve(), output_path.resolve()]
    assert output_path.read_bytes() == b"old-gds"
    assert log_path.read_text(encoding="utf-8") == _SUCCESS_LOG


@pytest.mark.parametrize(
    ("cleanup_policy", "succeeds", "run_dir_retained"),
    [
        ("success", True, False),
        ("success", False, True),
        ("always", True, False),
        ("always", False, False),
        ("never", True, True),
        ("never", False, True),
    ],
)
def test_export_gds_local_cleanup_policy_controls_run_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_policy: str,
    succeeds: bool,
    run_dir_retained: bool,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / f"{cleanup_policy}-{succeeds}" / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = (
        _artifact_client()
        if succeeds
        else _artifact_client(log_text=_TERMINAL_LOG, gds=None)
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy=cleanup_policy,
    )

    assert result.status == (
        ExecutionStatus.SUCCESS if succeeds else ExecutionStatus.FAILURE
    )
    assert result.local_log_path == output_path.with_name("top.xstream.log").resolve()
    assert (result.local_run_dir is not None) is run_dir_retained
    if result.local_run_dir is not None:
        assert result.local_run_dir.is_dir()


def test_export_gds_always_cleanup_removes_run_after_recovered_diagnostic_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(
        log_text=None,
        gds=None,
        response=VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["explicit SKILL failure"],
        ),
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="always",
    )

    assert result.reason == GdsExportReason.SKILL_ERROR
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert "explicit SKILL failure" in result.local_log_path.read_text(
        encoding="utf-8"
    )
    assert result.local_run_dir is None


def test_export_gds_prefers_late_xstream_log_over_synthetic_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    staged_log: list[Path] = []

    def execute(skill: str, _timeout: float) -> VirtuosoResult:
        _run_dir, _gds_path, log_path = _request_artifacts(skill)
        staged_log.append(log_path)
        return VirtuosoResult(
            status=ExecutionStatus.ERROR,
            errors=["explicit SKILL failure"],
        )

    client = _FakeLocalClient(execute)
    real_diagnostic = streamout._diagnostic_log_text

    def producer_wins_race(inputs: object, errors: list[str]) -> str:
        staged_log[0].write_text(_SUCCESS_LOG, encoding="utf-8")
        return real_diagnostic(inputs, errors)

    monkeypatch.setattr(
        streamout,
        "_diagnostic_log_text",
        producer_wins_race,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="never",
    )

    assert result.reason == GdsExportReason.SKILL_ERROR
    assert "explicit SKILL failure" in result.errors
    assert result.log_result is not None
    assert result.log_result.completed is True
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert result.local_log_path.read_text(encoding="utf-8") == _SUCCESS_LOG
    assert staged_log[0].read_text(encoding="utf-8") == _SUCCESS_LOG


def test_export_gds_always_cleanup_retains_run_when_log_cannot_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(log_text=_TERMINAL_LOG, gds=None)
    cleanup_calls: list[Path] = []

    def fail_publish(_source: Path, _destination: Path) -> None:
        raise OSError("publication unavailable")

    monkeypatch.setattr(streamout, "_publish_file", fail_publish)
    monkeypatch.setattr(
        streamout.shutil,
        "rmtree",
        lambda path: cleanup_calls.append(Path(path)),
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="always",
    )

    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.local_log_path is None
    assert result.local_run_dir is not None
    assert result.local_run_dir.is_dir()
    assert cleanup_calls == []


@pytest.mark.parametrize("cleanup_policy", ["success", "always"])
def test_local_cleanup_failure_preserves_status_and_reports_run_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_policy: str,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()

    def fail_cleanup(_path: Path) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr(streamout.shutil, "rmtree", fail_cleanup)

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy=cleanup_policy,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.timed_out is False
    assert result.local_gds_path == output_path.resolve()
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert result.local_run_dir is not None
    assert result.local_run_dir.is_dir()
    assert result.errors == ()
    assert any("local XStream cleanup failed" in warning for warning in result.warnings)
    assert any("cleanup denied" in warning for warning in result.warnings)


@pytest.mark.parametrize("cleanup_policy", ["success", "never"])
def test_gds_commit_crossing_deadline_warns_and_retains_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_policy: str,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()
    real_publish = streamout._publish_file

    def consume_budget(
        source: Path,
        destination: Path,
        *,
        validator: Callable[[], None] | None = None,
    ) -> None:
        if validator is None:
            real_publish(source, destination)
        else:
            real_publish(source, destination, validator=validator)
        if destination == output_path.resolve():
            clock.now = 4.0

    def forbidden_cleanup(_path: Path) -> None:
        raise AssertionError("cleanup called after total deadline")

    monkeypatch.setattr(streamout, "_publish_file", consume_budget)
    monkeypatch.setattr(streamout.shutil, "rmtree", forbidden_cleanup)

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy=cleanup_policy,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.timed_out is False
    assert result.local_gds_path == output_path.resolve()
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert result.local_run_dir is not None
    assert output_path.read_bytes() == b"current-run-gds"
    assert result.errors == ()
    assert (
        "GDS publication completed after the export deadline; "
        "local staging retained"
    ) in result.warnings
    if cleanup_policy == "success":
        assert "cleanup skipped because the export deadline expired" in result.warnings


def test_export_gds_cleanup_budget_exhaustion_preserves_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(log_text=_TERMINAL_LOG, gds=None)
    real_classify = streamout._classify_export

    def expire_after_classification(**observations: object) -> object:
        classified = real_classify(**observations)
        clock.now = 4.0
        return classified

    def forbidden_cleanup(_path: Path) -> None:
        raise AssertionError("cleanup called without remaining budget")

    monkeypatch.setattr(
        streamout,
        "_classify_export",
        expire_after_classification,
    )
    monkeypatch.setattr(streamout.shutil, "rmtree", forbidden_cleanup)

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="always",
    )

    assert result.status == ExecutionStatus.FAILURE
    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.timed_out is False
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert result.local_run_dir is not None
    assert result.local_run_dir.is_dir()
    assert not any("budget exhausted" in error for error in result.errors)
    assert "cleanup skipped because the export deadline expired" in result.warnings


def test_export_gds_cleanup_crossing_deadline_preserves_success_with_removed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()
    real_cleanup = streamout.shutil.rmtree

    def consume_deadline(path: Path) -> None:
        real_cleanup(path)
        clock.now = 4.0

    monkeypatch.setattr(streamout.shutil, "rmtree", consume_deadline)

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.timed_out is False
    assert result.local_gds_path == output_path.resolve()
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert result.local_run_dir is None
    assert result.errors == ()
    assert (
        "local XStream cleanup completed after the export deadline"
        in result.warnings
    )


def test_export_gds_cleanup_failure_after_deadline_preserves_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()

    def fail_after_deadline(_path: Path) -> None:
        clock.now = 4.0
        raise OSError("late cleanup failure")

    monkeypatch.setattr(streamout.shutil, "rmtree", fail_after_deadline)

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.reason == GdsExportReason.COMPLETED
    assert result.timed_out is False
    assert result.local_run_dir is not None
    assert result.local_run_dir.is_dir()
    assert result.errors == ()
    assert any("late cleanup failure" in warning for warning in result.warnings)


def test_export_gds_setup_error_is_structured_and_skips_skill_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker\n", encoding="utf-8")
    output_path = blocker / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.STAGING_ERROR
    assert result.timed_out is False
    assert result.local_gds_path is None
    assert result.local_log_path is None
    assert client.skill_calls == []


def test_export_gds_observation_read_error_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")

    def execute(skill: str, _timeout: float) -> VirtuosoResult:
        _run_dir, _gds_path, log_path = _request_artifacts(skill)
        log_path.mkdir()
        return _started_result()

    client = _FakeLocalClient(execute)

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.STAGING_ERROR
    assert result.timed_out is False
    assert result.local_gds_path is None
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert "xstream.log" in result.local_log_path.read_text(encoding="utf-8")
    assert any("xstream.log" in error for error in result.errors)


def test_export_gds_prefinal_budget_exhaustion_skips_skill_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _SequenceClock(0.0, 4.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.STAGING_ERROR
    assert result.timed_out is True
    assert client.skill_calls == []


def test_export_gds_prefinal_expiry_after_staging_uses_reserve_to_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()
    real_renderer = streamout.xstream_export_gds_skill

    def consume_prefinal_budget(request: object) -> str:
        skill = real_renderer(request)
        clock.now = 3.0
        return skill

    monkeypatch.setattr(
        streamout,
        "xstream_export_gds_skill",
        consume_prefinal_budget,
    )

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="always",
    )

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.STAGING_ERROR
    assert result.timed_out is True
    assert client.skill_calls == []
    assert result.local_log_path == output_path.with_name("top.xstream.log")
    assert "budget exhausted" in result.local_log_path.read_text(
        encoding="utf-8"
    )
    assert result.local_run_dir is None


def test_export_gds_skill_timeout_is_capped_by_positive_prefinal_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client()

    result = _run_export(
        client,
        output_path,
        stream_map,
        timeout=2.0,
        finalization_reserve=1.0,
        skill_timeout=5.0,
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert len(client.skill_calls) == 1
    assert client.skill_calls[0][1] == 1.0


def test_export_gds_total_budget_exhaustion_blocks_finalization_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    output_path.write_bytes(b"old-gds")
    stream_map = _write_stream_map(tmp_path / "layers.map")

    def execute(skill: str, _timeout: float) -> object:
        _write_artifacts(skill)
        clock.now = 4.0
        return {
            "status": "error",
            "errors": ["SKILL execution timeout in Virtuoso"],
        }

    client = _FakeLocalClient(execute)

    def forbidden_publish(_source: Path, _destination: Path) -> None:
        raise AssertionError("publication called after total deadline")

    def forbidden_cleanup(_path: Path) -> None:
        raise AssertionError("cleanup called after total deadline")

    monkeypatch.setattr(streamout, "_publish_file", forbidden_publish)
    monkeypatch.setattr(streamout.shutil, "rmtree", forbidden_cleanup)

    result = _run_export(client, output_path, stream_map)

    assert result.status == ExecutionStatus.ERROR
    assert result.reason == GdsExportReason.PUBLICATION_ERROR
    assert result.timed_out is True
    assert result.local_gds_path is None
    assert result.local_log_path is None
    assert result.local_run_dir is not None
    assert output_path.read_bytes() == b"old-gds"


def test_export_gds_unvalidated_gds_cleanup_failure_only_warns_and_retains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock(0.0)
    _use_fake_time(monkeypatch, clock)
    output_path = tmp_path / "top.gds"
    stream_map = _write_stream_map(tmp_path / "layers.map")
    client = _artifact_client(log_text=_TERMINAL_LOG, gds=b"unvalidated")
    real_unlink = Path.unlink

    def fail_staged_gds_unlink(
        path: Path,
        missing_ok: bool = False,
    ) -> None:
        if path.name == "output.gds":
            raise OSError("staged GDS cleanup denied")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_staged_gds_unlink)

    result = _run_export(
        client,
        output_path,
        stream_map,
        cleanup_policy="never",
    )

    assert result.reason == GdsExportReason.XSTREAM_FAILURE
    assert result.local_run_dir is not None
    assert (result.local_run_dir / "output.gds").read_bytes() == b"unvalidated"
    assert any("staged GDS cleanup denied" in warning for warning in result.warnings)

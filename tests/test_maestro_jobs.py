from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.maestro import (
    MaestroJobManager,
    MaestroJobSubmissionError,
)
from virtuoso_bridge.virtuoso.maestro import jobs as maestro_jobs
from virtuoso_bridge.virtuoso.requests import RequestHandle, RequestRecoveryError
from virtuoso_bridge.virtuoso.maestro.reader.state import get_session_state


class Result:
    def __init__(self, returncode=0, stdout="", stderr="") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRunner:
    host = "gui.example"
    user = "designer"

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.uploads: dict[str, str] = {}
        self.status_output = (
            "exists=1\ncompleted=0\nalive=1\nprocess_match=1\n"
            "process_start=linux:777\nmarker=\ncallback_session=\n"
            "callback_history=\npid=4242\n"
        )
        self.closed = False
        self.reserve_error = False

    def run_command(self, command: str, **_kwargs) -> Result:
        self.commands.append(command)
        if command.startswith("host=$(hostname"):
            return Result(stdout=f"host={self.host}\naccount={self.user}\n")
        if command == "cat /proc/4242/stat 2>/dev/null":
            fields = ["S"] + ["0"] * 18 + ["777"]
            return Result(stdout=f"4242 (virtuoso) {' '.join(fields)}\n")
        if self.reserve_error and "remote run id already exists" in command:
            return Result(returncode=2, stderr="remote run id already exists")
        if "printf 'exists=%s" in command:
            return Result(stdout=self.status_output)
        if command.startswith("tail -n"):
            return Result(stdout="submitted Interactive.7\n")
        return Result()

    def upload_text(self, content: str, remote_path: str) -> Result:
        self.uploads[remote_path] = content
        return Result()

    def close(self) -> None:
        self.closed = True


class PosixShellRunner:
    """Execute generated remote commands in one disposable local POSIX tree."""

    host = "fixture-gui"
    user = "fixture-user"

    def run_command(self, command: str, timeout=5, **_kwargs) -> Result:
        completed = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=min(timeout, 5),
            check=False,
        )
        return Result(completed.returncode, completed.stdout, completed.stderr)

    def upload_text(self, content: str, remote_path: str) -> Result:
        target = Path(remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return Result()

    def close(self) -> None:
        pass


class FakeDialogs:
    def __init__(self, status="clear", *, require_local_gui=False, pid=4242) -> None:
        self.status = status
        self.require_local_gui = require_local_gui
        self.pid = pid
        self.calls: list[dict[str, object]] = []

    def enable_guard(self, **kwargs):
        self.calls.append(kwargs)
        if self.require_local_gui and kwargs.get("local_gui") is not True:
            raise ValueError("local mode requires local_gui=True")
        return SimpleNamespace(status=self.status, target=SimpleNamespace(pid=self.pid))


class FakeRequests:
    def __init__(self) -> None:
        self.result = None
        self.calls = []

    def receipt(self, handle, *, timeout):
        self.calls.append((handle, timeout))
        return self.result


class FakeMaestro:
    def __init__(self, *, history='"Interactive.7"', start_error=None) -> None:
        self.history = history
        self.start_error = start_error
        self.run_calls = 0
        self.state_calls = 0

    def get_session_state(self, *, session, timeout):
        self.state_calls += 1
        return SimpleNamespace(
            context="gui",
            access="editing",
            lib="LIB",
            cell="TB",
            view="maestro",
            application="assembler",
            unsaved=False,
        )

    def run_simulation(self, **_kwargs):
        self.run_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return self.history


class FakeClient:
    def __init__(
        self,
        runner=None,
        *,
        history='"Interactive.7"',
        start_error=None,
    ) -> None:
        self.gui_runner = runner
        self._tunnel = SimpleNamespace(_profile="lab") if runner is not None else None
        self.dialogs = FakeDialogs(
            require_local_gui=runner is None,
            pid=4242 if runner is not None else os.getpid(),
        )
        self.maestro = FakeMaestro(history=history, start_error=start_error)
        self.requests = FakeRequests()
        self.skill_calls: list[str] = []
        self.callback_errors: list[str] = []

    def execute_skill(self, code, **_kwargs):
        self.skill_calls.append(code)
        return SimpleNamespace(errors=self.callback_errors, output="t")


def remote_manager(
    tmp_path: Path, runner: RecordingRunner, client=None
) -> MaestroJobManager:
    return MaestroJobManager(
        runner,
        work_root="/tmp/bridge/maestro jobs",
        transport="ssh",
        local_root=tmp_path / "runs",
        profile="lab",
        client=client,
    )


def test_remote_submit_persists_handle_and_starts_exactly_once(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)

    job = jobs.submit(session="fnxSession4", run_id="long-run-001", run_mode="Interactive")

    assert client.maestro.run_calls == 1
    assert client.maestro.state_calls == 1
    assert len(client.skill_calls) == 1
    assert "completed" in client.skill_calls[0]
    assert "session=%L" in client.skill_calls[0]
    assert "history=%L" in client.skill_calls[0]
    assert "maestro jobs" in client.skill_calls[0]
    assert job.history == "Interactive.7"
    assert job.target == {
        "application": "assembler",
        "cell": "TB",
        "library": "LIB",
        "unsaved": False,
        "view": "maestro",
    }
    manifest = json.loads((job.local_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["phase"] == "submitted"
    assert manifest["schema"] == 2
    assert manifest["transport"] == "ssh"
    assert manifest["virtuoso_pid"] == 4242
    assert manifest["process_start"] == "linux:777"
    assert manifest["endpoint"] == {
        "account": "designer",
        "configured_gui_host": "gui.example",
        "namespace": "/tmp/bridge/maestro jobs",
        "observed_gui_host": "gui.example",
        "transport": "ssh",
    }
    assert f"{job.work_dir}/manifest.json" in runner.uploads
    assert client.dialogs.calls == [
        {"local_gui": False, "protect_inflight": True, "timeout": 30}
    ]


def test_remote_run_id_is_reserved_with_atomic_mkdir(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)

    jobs.submit(session="fnxSession4", run_id="atomic-run-001")

    reserve = runner.commands[0]
    assert "test -e" not in reserve
    assert "mkdir -p '/tmp/bridge/maestro jobs/jobs'" in reserve
    assert "mkdir '/tmp/bridge/maestro jobs/jobs/atomic-run-001'" in reserve


def test_remote_run_id_conflict_stops_before_simulation_start(tmp_path) -> None:
    runner = RecordingRunner()
    runner.reserve_error = True
    client = FakeClient(runner)

    with pytest.raises(MaestroJobSubmissionError) as caught:
        remote_manager(tmp_path, runner, client).submit(
            session="fnxSession4", run_id="remote-conflict-001"
        )

    assert caught.value.state == "failed"
    assert client.maestro.run_calls == 0
    assert runner.uploads == {}


def test_status_reloads_without_client_and_never_uses_skill(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    submitted = remote_manager(tmp_path, runner, client).submit(
        session="fnxSession4", run_id="reload-run-001"
    )
    skill_count = len(client.skill_calls)
    observer = remote_manager(tmp_path, runner)

    reloaded = observer.load(submitted.run_id)
    status = observer.status(reloaded)

    assert status.state == "running"
    assert status.history == "Interactive.7"
    assert len(client.skill_calls) == skill_count
    assert client.maestro.run_calls == 1


def test_completion_marker_overrides_unknown_submission(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner, start_error=TimeoutError("ack timed out"))
    jobs = remote_manager(tmp_path, runner, client)

    with pytest.raises(MaestroJobSubmissionError) as caught:
        jobs.submit(session="fnxSession4", run_id="unknown-run-001")

    assert caught.value.state == "unknown"
    assert client.maestro.run_calls == 1
    runner.status_output = (
        "exists=1\ncompleted=1\nalive=1\nprocess_match=1\n"
        "process_start=linux:777\nmarker=completed\n"
        "callback_session=fnxSession4\ncallback_history=Interactive.7\npid=4242\n"
    )
    status = remote_manager(tmp_path, runner).status(jobs.load("unknown-run-001"))
    assert status.state == "completed"
    assert status.completion == "completed"


def test_known_unsent_start_is_failed_and_not_retried(tmp_path) -> None:
    runner = RecordingRunner()
    error = RuntimeError("preflight blocked")
    error.result = SimpleNamespace(metadata={"request_sent": False})
    client = FakeClient(runner, start_error=error)

    with pytest.raises(MaestroJobSubmissionError) as caught:
        remote_manager(tmp_path, runner, client).submit(
            session="fnxSession4", run_id="unsent-run-001"
        )

    assert caught.value.state == "failed"
    assert client.maestro.run_calls == 1


def test_callback_setup_failure_is_failed_and_never_starts(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    client.callback_errors = ["bad callback"]
    jobs = remote_manager(tmp_path, runner, client)

    with pytest.raises(MaestroJobSubmissionError) as caught:
        jobs.submit(session="fnxSession4", run_id="callback-fail-001")

    assert caught.value.state == "failed"
    assert client.maestro.run_calls == 0
    status = remote_manager(tmp_path, runner).status(jobs.load("callback-fail-001"))
    assert status.state == "failed"


def test_blocked_guard_fails_before_state_probe_or_start(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    client.dialogs.status = "blocked"

    with pytest.raises(MaestroJobSubmissionError) as caught:
        remote_manager(tmp_path, runner, client).submit(
            session="fnxSession4", run_id="blocked-run-001"
        )

    assert caught.value.state == "failed"
    assert client.maestro.state_calls == 0
    assert client.maestro.run_calls == 0


def test_nil_history_is_unknown_and_not_retried(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner, history="nil")

    with pytest.raises(MaestroJobSubmissionError) as caught:
        remote_manager(tmp_path, runner, client).submit(
            session="fnxSession4", run_id="nil-history-001"
        )

    assert caught.value.state == "unknown"
    assert client.maestro.run_calls == 1


def test_missing_remote_directory_is_terminal_missing(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)
    job = jobs.submit(session="fnxSession4", run_id="missing-run-001")
    runner.status_output = "exists=0\ncompleted=0\nalive=0\nmarker=\npid=\n"

    assert remote_manager(tmp_path, runner).status(job).state == "missing"


def test_duplicate_local_run_id_is_rejected_before_second_start(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)
    jobs.submit(session="fnxSession4", run_id="duplicate-run-001")

    with pytest.raises(FileExistsError):
        jobs.submit(session="fnxSession4", run_id="duplicate-run-001")
    assert client.maestro.run_calls == 1


def test_log_is_explicitly_lifecycle_only(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)
    job = jobs.submit(session="fnxSession4", run_id="event-log-001")

    assert jobs.log(job, lines=10) == "submitted Interactive.7\n"
    assert "/events.log" in runner.commands[-1]


@pytest.fixture
def portable_local_process_identity(monkeypatch):
    if os.name != "nt" and not Path("/proc/self/stat").is_file():
        # These test filesystem/authorization behavior, not a native Cadence process.
        monkeypatch.setattr(MaestroJobManager, "_process_start_identity",
                            lambda self, pid: f"fixture:{pid}")


def test_local_transport_submits_and_observes_completion_without_ssh(
    tmp_path, portable_local_process_identity
) -> None:
    client = FakeClient()
    jobs = MaestroJobManager.from_client(
        client,
        local_root=tmp_path / "runs",
        local_work_root=tmp_path / "local work",
        local_gui=True,
    )

    job = jobs.submit(session="fnxSession4", run_id="local-run-001")
    assert job.transport == "local"
    assert client.maestro.run_calls == 1
    work_dir = Path(job.work_dir)
    (work_dir / "completed").write_text(
        "completed\nsession=fnxSession4\nhistory=Interactive.7\n",
        encoding="utf-8",
    )

    observer = MaestroJobManager.local(
        local_root=tmp_path / "runs", work_root=tmp_path / "local work"
    )
    status = observer.status(observer.load(job.run_id))
    assert status.state == "completed"
    assert status.transport == "local"
    assert observer.log(job).startswith("submitted Interactive.7")
    assert client.dialogs.calls == [
        {"local_gui": True, "protect_inflight": True, "timeout": 30}
    ]


def test_from_client_rejects_profile_different_from_connected_tunnel(tmp_path) -> None:
    client = FakeClient(RecordingRunner())

    with pytest.raises(ValueError, match="does not match"):
        MaestroJobManager.from_client(
            client,
            local_root=tmp_path / "runs",
            remote_root="/tmp/bridge/jobs",
            profile="other",
        )


def test_from_client_requires_explicit_local_gui_authorization(
    tmp_path, portable_local_process_identity
) -> None:
    forwarded_client = FakeClient()

    with pytest.raises(ValueError, match="local_gui=True"):
        MaestroJobManager.from_client(
            forwarded_client,
            local_root=tmp_path / "runs",
            local_work_root=tmp_path / "local work",
        )

    manager = MaestroJobManager.from_client(
        forwarded_client,
        local_root=tmp_path / "runs",
        local_work_root=tmp_path / "local work",
        local_gui=True,
    )
    assert manager.submit(session="fnxSession4", run_id="local-explicit-001").transport == "local"


def test_unknown_start_persists_and_reconciles_original_request(tmp_path) -> None:
    runner = RecordingRunner()
    handle = RequestHandle(
        request_id="a" * 32,
        daemon_instance="b" * 32,
        virtuoso_pid=4242,
    )
    uncertain = VirtuosoResult(
        status=ExecutionStatus.ERROR,
        errors=["request acknowledgement unavailable"],
        metadata={
            "request_handle": handle.model_dump(),
            "request_state": "running",
            "outcome": "unknown",
            "request_sent": True,
            "phase": "wait_timeout",
        },
    )
    submitting_client = FakeClient(
        runner, start_error=RequestRecoveryError(uncertain)
    )
    submitting = remote_manager(tmp_path, runner, submitting_client)

    with pytest.raises(MaestroJobSubmissionError) as caught:
        submitting.submit(session="fnxSession4", run_id="recover-run-001")

    assert caught.value.request_handle == handle
    observer = remote_manager(tmp_path, runner)
    reloaded = observer.load("recover-run-001")
    assert reloaded.request_handle == handle
    assert reloaded.request_evidence["request_state"] == "running"

    recovery_client = FakeClient(runner)
    recovery_client.requests.result = VirtuosoResult(
        status=ExecutionStatus.SUCCESS,
        output='"Interactive.9"',
        metadata={
            "request_handle": handle.model_dump(),
            "request_state": "completed",
            "outcome": "completed",
            "request_sent": True,
        },
    )
    reconciled = observer.reconcile(reloaded, client=recovery_client)

    assert reconciled.history == "Interactive.9"
    assert reconciled.request_handle == handle
    assert recovery_client.requests.calls == [(handle, 10)]
    assert recovery_client.maestro.run_calls == 0


@pytest.mark.parametrize(
    "host,user,work_root",
    [
        ("other-gui.example", "designer", "/tmp/bridge/maestro jobs"),
        ("gui.example", "other-user", "/tmp/bridge/maestro jobs"),
        ("gui.example", "designer", "/tmp/other-client/maestro jobs"),
    ],
)
def test_status_and_log_reject_endpoint_identity_drift(
    tmp_path, host, user, work_root
) -> None:
    runner = RecordingRunner()
    source = remote_manager(tmp_path, runner, FakeClient(runner))
    job = source.submit(session="fnxSession4", run_id="identity-run-001")
    other = RecordingRunner()
    other.host = host
    other.user = user
    manager = MaestroJobManager(
        other,
        work_root=work_root,
        transport="ssh",
        local_root=tmp_path / "runs",
        profile="lab",
    )

    with pytest.raises(ValueError, match="host/account/namespace"):
        manager.status(job)
    with pytest.raises(ValueError, match="host/account/namespace"):
        manager.log(job)
    assert other.commands == []


def test_pid_reuse_does_not_report_running(tmp_path) -> None:
    runner = RecordingRunner()
    manager = remote_manager(tmp_path, runner, FakeClient(runner))
    job = manager.submit(session="fnxSession4", run_id="pid-reuse-001")
    runner.status_output = (
        "exists=1\ncompleted=0\nalive=1\nprocess_match=0\n"
        "process_start=linux:777\nmarker=\ncallback_session=\n"
        "callback_history=\npid=4242\n"
    )

    assert manager.status(job).state == "unknown"


def test_callback_identity_mismatch_is_not_completed(tmp_path) -> None:
    runner = RecordingRunner()
    manager = remote_manager(tmp_path, runner, FakeClient(runner))
    job = manager.submit(session="fnxSession4", run_id="callback-id-001")
    runner.status_output = (
        "exists=1\ncompleted=1\nalive=1\nprocess_match=1\n"
        "process_start=linux:777\nmarker=completed\n"
        "callback_session=otherSession\ncallback_history=Interactive.7\npid=4242\n"
    )

    status = manager.status(job)
    assert status.state == "unknown"
    assert "did not match" in status.diagnostics[-1]


def test_schema_one_handle_requires_explicit_endpoint_migration(tmp_path) -> None:
    local_root = tmp_path / "runs"
    local_dir = local_root / "legacy-run-001"
    local_dir.mkdir(parents=True)
    manifest = {
        "schema": 1,
        "kind": "maestro-simulation",
        "run_id": "legacy-run-001",
        "transport": "ssh",
        "profile": "lab",
        "session": "fnxSession4",
        "history": "Interactive.1",
        "virtuoso_pid": 4242,
        "target": {},
        "work_dir": "/tmp/bridge/maestro jobs/jobs/legacy-run-001",
        "diagnostics": [],
    }
    (local_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    manager = remote_manager(tmp_path, RecordingRunner())

    with pytest.raises(RuntimeError, match="migrate_legacy=True"):
        manager.load("legacy-run-001")
    migrated = manager.load("legacy-run-001", migrate_legacy=True)

    assert migrated.endpoint == manager._endpoint
    assert migrated.process_start is None
    assert json.loads((local_dir / "manifest.json").read_text())["schema"] == 2


def test_post_start_manifest_failure_returns_durable_unknown_error(
    tmp_path, monkeypatch
) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)
    real_write = MaestroJobManager._write_manifest

    def fail_submitted(local_dir, manifest):
        if manifest.get("phase") == "submitted":
            raise OSError("manifest storage unavailable")
        return real_write(local_dir, manifest)

    monkeypatch.setattr(
        MaestroJobManager, "_write_manifest", staticmethod(fail_submitted)
    )

    with pytest.raises(MaestroJobSubmissionError) as caught:
        jobs.submit(session="fnxSession4", run_id="persist-fail-001")

    assert caught.value.run_id == "persist-fail-001"
    assert caught.value.state == "unknown"
    assert "request was not repeated" in str(caught.value)
    assert client.maestro.run_calls == 1
    assert jobs.status(jobs.load("persist-fail-001")).state == "unknown"


@pytest.mark.skipif(os.name != "nt", reason="Windows process probe")
def test_windows_pid_probe_does_not_use_os_kill(monkeypatch) -> None:
    def unsafe_kill(*_args):
        raise AssertionError("os.kill must not be used for Windows PID probing")

    monkeypatch.setattr(maestro_jobs.os, "kill", unsafe_kill)

    assert MaestroJobManager._pid_alive(os.getpid()) is True


def test_list_skips_structurally_invalid_manifest(tmp_path) -> None:
    local_root = tmp_path / "runs"
    bad_dir = local_root / "bad-run-001"
    bad_dir.mkdir(parents=True)
    (bad_dir / "manifest.json").write_text(
        json.dumps({"kind": "wrong", "run_id": "bad-run-001"}),
        encoding="utf-8",
    )

    assert MaestroJobManager.local(local_root=local_root).list() == []


def test_manager_rejects_job_from_other_transport(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    remote = remote_manager(tmp_path, runner, client)
    job = remote.submit(session="fnxSession4", run_id="transport-run-001")
    local = MaestroJobManager.local(local_root=tmp_path / "runs")

    with pytest.raises(ValueError, match="transport"):
        local.status(job)


def test_manager_rejects_job_from_other_profile(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    source = remote_manager(tmp_path, runner, client)
    job = source.submit(session="fnxSession4", run_id="profile-run-001")
    wrong_profile = MaestroJobManager(
        runner,
        work_root="/tmp/bridge/maestro jobs",
        transport="ssh",
        local_root=tmp_path / "runs",
        profile="other",
    )

    with pytest.raises(ValueError, match="profile"):
        wrong_profile.status(job)


def test_manifest_replace_retries_brief_permission_error(tmp_path, monkeypatch) -> None:
    source = tmp_path / "manifest.json.tmp"
    destination = tmp_path / "manifest.json"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    real_replace = maestro_jobs.os.replace
    calls = 0

    def flaky_replace(left, right):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("brief scanner lock")
        return real_replace(left, right)

    monkeypatch.setattr(maestro_jobs.os, "replace", flaky_replace)

    MaestroJobManager._atomic_replace(source, destination)

    assert calls == 2
    assert destination.read_text(encoding="utf-8") == "new"


@pytest.mark.skipif(
    os.name != "posix" or shutil.which("sh") is None,
    reason="requires a real POSIX shell and /proc process identity",
)
def test_real_shell_conflict_preserves_existing_remote_job(tmp_path) -> None:
    runner = PosixShellRunner()
    work_root = tmp_path / "remote root's maestro jobs"
    first = MaestroJobManager(
        runner,
        work_root=work_root.as_posix(),
        transport="ssh",
        local_root=tmp_path / "first-local",
        profile="lab",
    )
    remote_dir = work_root / "jobs" / "shell-conflict-001"
    first._reserve_work_dir(remote_dir.as_posix(), local_dir=tmp_path / "first-local")
    first._persist_text(
        remote_dir.as_posix(), "manifest.json", '{"owner":"first"}\n', required=True
    )
    first._append_event(remote_dir.as_posix(), "submitted Interactive.'7")
    (remote_dir / "completed").write_text(
        "completed\nsession=fnxSession4\nhistory=Interactive.7\n",
        encoding="utf-8",
    )
    before = {
        path.name: path.read_bytes() for path in remote_dir.iterdir() if path.is_file()
    }

    second_client = FakeClient(runner)
    second_client.dialogs.pid = os.getpid()
    second = MaestroJobManager(
        runner,
        work_root=work_root.as_posix(),
        transport="ssh",
        local_root=tmp_path / "second-local",
        profile="lab",
        client=second_client,
    )
    with pytest.raises(MaestroJobSubmissionError, match="already exists"):
        second.submit(session="fnxSession4", run_id="shell-conflict-001")

    after = {
        path.name: path.read_bytes() for path in remote_dir.iterdir() if path.is_file()
    }
    assert after == before
    observed = first._observe_work_dir(remote_dir.as_posix())
    assert observed["completed"] == "1"
    assert observed["callback_session"] == "fnxSession4"
    assert observed["callback_history"] == "Interactive.7"


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("pid,start", [(9999, "linux:999"), (4242, "linux:888")])
def test_observed_process_must_match_durable_job(tmp_path, completed, pid, start):
    runner = RecordingRunner()
    manager = remote_manager(tmp_path, runner, FakeClient(runner))
    job = manager.submit(session="fnxSession4", run_id="durable-instance-001")
    runner.status_output = (
        f"exists=1\ncompleted={int(completed)}\nalive=1\nprocess_match=1\n"
        f"process_start={start}\nmarker={'completed' if completed else ''}\n"
        f"callback_session=fnxSession4\ncallback_history=Interactive.7\npid={pid}\n"
    )
    assert manager.status(job).state == "unknown"


def test_missing_original_process_identity_cannot_report_running(tmp_path):
    runner = RecordingRunner()
    manager = remote_manager(tmp_path, runner, FakeClient(runner))
    job = manager.submit(session="fnxSession4", run_id="missing-instance-001")
    assert manager.status(replace(job, process_start=None)).state == "unknown"


@pytest.mark.parametrize("method", ["status", "log"])
def test_same_manager_reconnect_identity_is_checked_before_file_read(tmp_path, method):
    runner = RecordingRunner()
    manager = remote_manager(tmp_path, runner, FakeClient(runner))
    job = manager.submit(session="fnxSession4", run_id="reconnect-identity-001")
    original = runner.run_command
    reads = []

    def reconnected(command, **kwargs):
        if command.startswith("host=$(hostname"):
            return Result(stdout="host=other-real-gui\naccount=designer\n")
        reads.append(command)
        return original(command, **kwargs)

    runner.run_command = reconnected
    with pytest.raises(ValueError, match="identity"):
        getattr(manager, method)(job)
    assert reads == []


@pytest.mark.parametrize("phase", ["session_state", "callback_setup"])
def test_pending_preflight_receipt_is_retained_without_becoming_run_history(tmp_path, phase):
    runner = RecordingRunner()
    client = FakeClient(runner)
    handle = RequestHandle(request_id="c" * 32, daemon_instance="d" * 32, virtuoso_pid=4242)
    pending = VirtuosoResult(
        status=ExecutionStatus.ERROR, errors=["preflight request still running"],
        metadata={"request_handle": handle.model_dump(), "request_state": "running",
                  "outcome": "unknown", "request_sent": True},
    )
    if phase == "session_state":
        client.execute_skill = lambda *args, **kwargs: pending
        client.maestro.get_session_state = lambda **kwargs: get_session_state(client, **kwargs)
    else:
        client.execute_skill = lambda *args, **kwargs: pending
    manager = remote_manager(tmp_path, runner, client)
    with pytest.raises(MaestroJobSubmissionError) as caught:
        manager.submit(session="fnxSession4", run_id="preflight-receipt-001")
    assert caught.value.state == "unknown"
    assert caught.value.request_handle == handle
    assert client.maestro.run_calls == 0
    job = manager.load("preflight-receipt-001")
    assert job.request_handle == handle
    assert job.request_evidence["maestro_phase"] == phase
    client.requests.result = pending
    still_pending = manager.reconcile(job, client=client)
    assert still_pending.request_evidence["maestro_phase"] == phase
    assert manager.status(still_pending).state == "unknown"
    assert client.maestro.run_calls == 0
    client.requests.result = VirtuosoResult(
        status=ExecutionStatus.SUCCESS, output="t",
        metadata={"request_handle": handle.model_dump(), "request_state": "completed",
                  "outcome": "completed", "request_sent": True},
    )
    reconciled = manager.reconcile(still_pending, client=client)
    assert reconciled.history is None
    assert manager.status(reconciled).state == "failed"
    assert client.maestro.run_calls == 0


@pytest.mark.skipif(os.name != "nt" and not Path("/proc/self/stat").is_file(),
                    reason="requires native Windows or Linux process identity")
def test_native_current_process_start_identity(tmp_path):
    manager = MaestroJobManager.local(local_root=tmp_path / "local")
    identity = manager._process_start_identity(os.getpid())
    assert identity and identity.startswith("windows:" if os.name == "nt" else "linux:")

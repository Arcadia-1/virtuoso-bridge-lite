from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge.spectre import runner


def test_local_spectre_defaults_to_artifact_dir(monkeypatch, tmp_path) -> None:
    netlist_dir = tmp_path / "repo-netlists"
    netlist_dir.mkdir()
    netlist = netlist_dir / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    monkeypatch.setenv("VB_OUTPUT_DIR", str(tmp_path / "artifacts"))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner._run_spectre_local(netlist=netlist, spectre_cmd="spectre")

    expected_cwd = tmp_path / "artifacts" / "spectre" / "tb_amp"
    assert calls[0][1]["cwd"] == str(expected_cwd)
    assert result.output_dir == expected_cwd
    assert expected_cwd.is_dir()
    assert not (netlist_dir / "tb_amp.raw").exists()


def test_local_spectre_honors_per_run_args_and_stages_includes(monkeypatch, tmp_path) -> None:
    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text('include "model.va"\n', encoding="utf-8")
    include = tmp_path / "model.va"
    include.write_text("module model; endmodule\n", encoding="utf-8")
    work_dir = tmp_path / "run"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    runner._run_spectre_local(
        netlist=netlist,
        params={"include_files": [include], "spectre_args": ["+aps"]},
        work_dir=work_dir,
    )

    assert "+aps" in calls[0][0]
    assert (work_dir / "model.va").read_text(encoding="utf-8") == "module model; endmodule\n"


def test_local_spectre_sources_configured_cadence_environment(monkeypatch, tmp_path) -> None:
    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    runner._run_spectre_local(
        netlist=netlist,
        work_dir=tmp_path / "run",
        cadence_cshrc="/eda/cadence.cshrc",
    )

    assert calls[0][0][:2] == ["sh", "-c"]
    assert "csh -f -c" in calls[0][0][2]
    assert "/eda/cadence.cshrc" in calls[0][0][2]


def _successful_run(output_dir: Path) -> runner._SpectreRunResult:
    output_dir.mkdir(parents=True)
    return runner._SpectreRunResult(
        success=True,
        output_dir=output_dir,
        returncode=0,
        stdout="",
        stderr="",
        error=None,
        metadata={},
    )


def test_synchronous_simulation_keeps_configured_work_dir(monkeypatch, tmp_path) -> None:
    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    work_dir = tmp_path / "run"
    calls = []

    def fake_run_spectre_local(**kwargs):
        calls.append(kwargs)
        return _successful_run(kwargs["work_dir"] / "tb_amp.raw")

    monkeypatch.setattr(runner, "_run_spectre_local", fake_run_spectre_local)
    simulator = runner.SpectreSimulator.local(work_dir=work_dir)

    result = simulator.run_simulation(netlist)

    assert result.ok
    assert calls[0]["work_dir"] == work_dir


def test_parallel_local_runs_use_unique_work_dirs(monkeypatch, tmp_path) -> None:
    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    work_dir = tmp_path / "runs"
    task_work_dirs = []

    def fake_run_spectre_local(**kwargs):
        task_work_dir = kwargs["work_dir"]
        task_work_dirs.append(task_work_dir)
        return _successful_run(task_work_dir / "tb_amp.raw")

    monkeypatch.setattr(runner, "_run_spectre_local", fake_run_spectre_local)
    simulator = runner.SpectreSimulator.local(work_dir=work_dir)

    results = simulator.run_parallel(
        [(netlist, {}), (netlist, {})],
        max_workers=2,
    )

    assert all(result.ok for result in results)
    assert len(set(task_work_dirs)) == 2
    assert all(path.parent == work_dir for path in task_work_dirs)
    assert all(re.fullmatch(r"tb_amp__[0-9a-f]{8}", path.name) for path in task_work_dirs)


def test_parallel_remote_downloads_use_unique_local_work_dirs(monkeypatch, tmp_path) -> None:
    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    work_dir = tmp_path / "downloads"
    task_work_dirs = []

    def fake_run_spectre_remote(**kwargs):
        task_work_dir = kwargs["base_output_dir"]
        task_work_dirs.append(task_work_dir)
        return _successful_run(task_work_dir / "tb_amp.raw")

    monkeypatch.setattr(runner, "_run_spectre_remote", fake_run_spectre_remote)
    simulator = runner.SpectreSimulator(
        remote_host="compute-host",
        remote_work_dir="/tmp/spectre-runs",
        work_dir=work_dir,
        ssh_runner=object(),
    )

    results = simulator.run_parallel(
        [(netlist, {}), (netlist, {})],
        max_workers=2,
    )

    assert all(result.ok for result in results)
    assert len(set(task_work_dirs)) == 2
    assert all(path.parent == work_dir for path in task_work_dirs)
    assert all(re.fullmatch(r"tb_amp__[0-9a-f]{8}", path.name) for path in task_work_dirs)


def test_parallel_batches_use_independent_scoped_executors(
    monkeypatch,
    tmp_path,
) -> None:
    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    created_workers = []
    shutdown_workers = []

    class RecordingExecutor(ThreadPoolExecutor):
        def __init__(self, max_workers: int) -> None:
            created_workers.append(max_workers)
            super().__init__(max_workers=max_workers)

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            shutdown_workers.append(self._max_workers)
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def fake_run_in_work_dir(netlist, params, work_dir):
        return runner.SimulationResult(status=runner.ExecutionStatus.SUCCESS)

    monkeypatch.setattr(runner, "ThreadPoolExecutor", RecordingExecutor)
    simulator = runner.SpectreSimulator.local(work_dir=tmp_path / "runs")
    monkeypatch.setattr(simulator, "_run_in_work_dir", fake_run_in_work_dir)

    first = simulator.run_parallel([(netlist, {}), (netlist, {})], max_workers=2)
    second = simulator.run_parallel([(netlist, {})], max_workers=1)

    assert all(result.ok for result in first + second)
    assert created_workers == [2, 1]
    assert shutdown_workers == [2, 1]


def test_explicit_parallel_pool_owns_its_lifecycle(monkeypatch, tmp_path) -> None:
    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")

    def fake_run_in_work_dir(netlist, params, work_dir):
        return runner.SimulationResult(status=runner.ExecutionStatus.SUCCESS)

    simulator = runner.SpectreSimulator.local(work_dir=tmp_path / "runs")
    monkeypatch.setattr(simulator, "_run_in_work_dir", fake_run_in_work_dir)

    with simulator.parallel_pool(max_workers=2) as pool:
        futures = [pool.submit(netlist), pool.submit(netlist)]
        results = pool.wait_all(futures)

    assert all(result.ok for result in results)
    with pytest.raises(RuntimeError, match="shut down"):
        pool.submit(netlist)


def test_parallel_worker_count_must_be_positive(tmp_path) -> None:
    simulator = runner.SpectreSimulator.local(work_dir=tmp_path / "runs")

    with pytest.raises(ValueError, match="max_workers must be at least 1"):
        simulator.run_parallel([], max_workers=0)
    with pytest.raises(ValueError, match="max_workers must be at least 1"):
        simulator.parallel_pool(max_workers=0)


def test_legacy_submit_pool_does_not_leak_into_batches(monkeypatch, tmp_path) -> None:
    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")

    def fake_run_in_work_dir(netlist, params, work_dir):
        return runner.SimulationResult(status=runner.ExecutionStatus.SUCCESS)

    simulator = runner.SpectreSimulator.local(work_dir=tmp_path / "runs")
    monkeypatch.setattr(simulator, "_run_in_work_dir", fake_run_in_work_dir)

    with pytest.deprecated_call():
        simulator.set_max_workers(2)
    assert simulator.run_parallel([(netlist, {})], max_workers=1)[0].ok
    with pytest.deprecated_call():
        simulator.set_max_workers(3)

    with pytest.deprecated_call():
        future = simulator.submit(netlist)
    assert future.result().ok
    with pytest.deprecated_call(), pytest.raises(
        RuntimeError,
        match="Cannot resize",
    ):
        simulator.set_max_workers(1)

    simulator.shutdown()
    with pytest.deprecated_call():
        simulator.set_max_workers(1)


def test_failed_remote_run_logs_spectre_output_instead_of_printing(
    monkeypatch, tmp_path, capsys, caplog
) -> None:
    from virtuoso_bridge.transport.ssh import CommandResult, RemoteTaskResult

    spectre_error = 'ERROR (SFE-868): "fixture.scs" 2: Cannot open the input file "missing.scs".'
    monkeypatch.setattr(
        runner,
        "run_remote_task",
        lambda *_args, **_kwargs: RemoteTaskResult(
            success=True, returncode=1, stdout=spectre_error, stderr="",
            remote_dir=None, error=None, timings={},
        ),
    )

    class _Runner:
        host = "eda-host"

        def download(self, _remote, local, recursive=False, **_kwargs):
            Path(local).mkdir(parents=True, exist_ok=True)
            return CommandResult(returncode=0, stdout="", stderr="")

    netlist = tmp_path / "fixture.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    caplog.set_level("INFO", logger="virtuoso_bridge.spectre.runner")

    run_result = runner._run_spectre_remote(
        netlist=netlist, params={}, runner=_Runner(), remote_work_dir="/remote/work",
        base_output_dir=tmp_path / "out", keep_remote_files=True,
        cadence_env=runner.CadenceEnvironment(""),
    )
    result = runner._build_simulation_result(run_result, "psfascii")

    assert capsys.readouterr().out == ""
    assert spectre_error in caplog.text
    assert "spectre" in caplog.text and "+log" in caplog.text
    assert any("SFE-868" in error or "file not found" in error for error in result.errors)


@pytest.mark.parametrize("keep", [True, False])
def test_kept_remote_run_reports_its_remote_paths(monkeypatch, tmp_path, keep) -> None:
    from virtuoso_bridge.transport.ssh import CommandResult, RemoteTaskResult

    monkeypatch.setattr(
        runner,
        "run_remote_task",
        lambda *_args, **kwargs: RemoteTaskResult(
            success=True, returncode=0, stdout="", stderr="",
            remote_dir=f"/remote/work/{kwargs['run_id']}", error=None, timings={},
        ),
    )
    removed = []

    class _Runner:
        host = "eda-host"

        def download(self, _remote, local, recursive=False, **_kwargs):
            Path(local).mkdir(parents=True, exist_ok=True)
            return CommandResult(returncode=0, stdout="", stderr="")

        def run_command(self, command, **_kwargs):
            removed.append(command)
            return CommandResult(returncode=0, stdout="", stderr="")

    netlist = tmp_path / "fixture.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    result = runner._run_spectre_remote(
        netlist=netlist, params={}, runner=_Runner(), remote_work_dir="/remote/work",
        base_output_dir=tmp_path / "out", keep_remote_files=keep,
        cadence_env=runner.CadenceEnvironment(""),
    )

    assert result.success
    if keep:
        remote_dir = result.metadata["remote_dir"]
        assert remote_dir.startswith("/remote/work/")
        assert result.metadata["remote_raw_dir"] == f"{remote_dir}/fixture.raw"
        assert removed == []
    else:
        # A deleted run directory is not offered as a usable path.
        assert "remote_dir" not in result.metadata
        assert "remote_raw_dir" not in result.metadata
        assert removed and removed[0].startswith("rm -rf /remote/work/")


def test_simulation_result_exposes_kept_remote_paths(monkeypatch, tmp_path) -> None:
    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    seen = {}

    def fake_run_spectre_remote(**kwargs):
        seen["keep"] = kwargs["keep_remote_files"]
        run = _successful_run(kwargs["base_output_dir"] / "tb_amp.raw")
        run.metadata.update(remote_dir="/tmp/spectre-runs/abcd1234",
                            remote_raw_dir="/tmp/spectre-runs/abcd1234/tb_amp.raw")
        return run

    monkeypatch.setattr(runner, "_run_spectre_remote", fake_run_spectre_remote)
    simulator = runner.SpectreSimulator(
        remote_host="compute-host", remote_work_dir="/tmp/spectre-runs",
        work_dir=tmp_path / "downloads", ssh_runner=object(), keep_remote_files=True,
    )

    result = simulator.run_simulation(netlist, {})

    assert seen["keep"] is True
    assert result.metadata["remote_raw_dir"] == "/tmp/spectre-runs/abcd1234/tb_amp.raw"
    assert result.metadata["remote_dir"] == "/tmp/spectre-runs/abcd1234"


@pytest.mark.parametrize("keep_remote_files", [False, True])
def test_remote_timeout_terminates_pid_and_cleans_quoted_dir(
    monkeypatch, tmp_path, keep_remote_files: bool
) -> None:
    from virtuoso_bridge.transport.ssh import CommandResult, RemoteTaskResult

    captured_exec: list[str] = []

    def fake_run_remote_task(_runner, *, work_dir_base, run_id, command, **_kwargs):
        captured_exec.append(command)
        return RemoteTaskResult(
            success=False,
            returncode=-1,
            stdout="",
            stderr="",
            remote_dir=f"{work_dir_base}/{run_id}",
            error="Remote command timed out after 5 seconds",
            timings={"upload_total": 0.1},
        )

    monkeypatch.setattr(runner, "run_remote_task", fake_run_remote_task)
    commands: list[str] = []

    class _Runner:
        host = "eda-host"

        def run_command(self, command, **_kwargs):
            commands.append(command)
            return CommandResult(returncode=0, stdout="", stderr="")

    netlist = tmp_path / "fixture.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")

    result = runner._run_spectre_remote(
        netlist=netlist,
        params={},
        runner=_Runner(),
        remote_work_dir="/remote/work dir",
        base_output_dir=tmp_path / "out",
        timeout=5,
        keep_remote_files=keep_remote_files,
        cadence_env=runner.CadenceEnvironment(""),
    )

    assert not result.success
    assert "timeout -k 5s 5s" in captured_exec[0]
    assert "spectre.pid" in captured_exec[0]
    assert any("spectre.pid" in cmd and "kill -TERM" in cmd for cmd in commands)
    if keep_remote_files:
        assert not any(cmd.startswith("rm -rf ") for cmd in commands)
    else:
        rm_cmds = [cmd for cmd in commands if cmd.startswith("rm -rf ")]
        assert len(rm_cmds) == 1
        assert rm_cmds[0].startswith("rm -rf '/remote/work dir/")
        # Termination must happen before deleting the directory holding spectre.pid.
        kill_idx = next(i for i, c in enumerate(commands) if "spectre.pid" in c)
        rm_idx = next(i for i, c in enumerate(commands) if c.startswith("rm -rf "))
        assert kill_idx < rm_idx


def test_remote_timeout_exit_124_fails_and_cleans_up(monkeypatch, tmp_path) -> None:
    from virtuoso_bridge.transport.ssh import CommandResult, RemoteTaskResult

    monkeypatch.setattr(
        runner,
        "run_remote_task",
        lambda *_a, **kw: RemoteTaskResult(
            success=True,
            returncode=124,
            stdout="",
            stderr="",
            remote_dir=f"{kw['work_dir_base']}/{kw['run_id']}",
            error=None,
            timings={},
        ),
    )
    commands: list[str] = []

    class _Runner:
        host = "eda-host"

        def run_command(self, command, **_kwargs):
            commands.append(command)
            return CommandResult(returncode=0, stdout="", stderr="")

    netlist = tmp_path / "fixture.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")

    result = runner._run_spectre_remote(
        netlist=netlist,
        params={},
        runner=_Runner(),
        remote_work_dir="/remote/work",
        base_output_dir=tmp_path / "out",
        timeout=5,
        keep_remote_files=False,
        cadence_env=runner.CadenceEnvironment(""),
    )

    assert not result.success
    assert result.returncode == 124
    assert "timed out" in (result.error or "").lower()
    assert any(cmd.startswith("rm -rf /remote/work/") for cmd in commands)


def test_remote_timeout_kills_real_hanging_process_and_removes_spaced_dir(tmp_path) -> None:
    import os
    import shlex
    import shutil
    import subprocess
    import sys

    if os.name == "nt" or not shutil.which("sh"):
        pytest.skip("requires POSIX sh")

    from virtuoso_bridge.transport.ssh import CommandResult

    pid_log = tmp_path / "spawned.pid"
    hang_script = tmp_path / "fake spectre.py"
    hang_script.write_text(
        f"import os, pathlib, time\n"
        f"pathlib.Path({str(pid_log)!r}).write_text(str(os.getpid()))\n"
        f"time.sleep(30)\n",
        encoding="utf-8",
    )

    class _LocalPosixRunner:
        host = "fixture-host"

        def upload_batch(self, uploads):
            for src, dst in uploads:
                target = Path(dst)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            return CommandResult(0, "", "")

        def run_command(self, command, timeout=None):
            completed = subprocess.run(
                ["sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    remote_base = tmp_path / "remote work dir"
    netlist = tmp_path / "fixture.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")

    result = runner._run_spectre_remote(
        netlist=netlist,
        params={},
        runner=_LocalPosixRunner(),
        remote_work_dir=str(remote_base),
        base_output_dir=tmp_path / "out",
        spectre_cmd=shlex.join([sys.executable, str(hang_script)]),
        timeout=1,
        keep_remote_files=False,
        cadence_env=runner.CadenceEnvironment(""),
    )

    assert not result.success
    assert "timed out" in (result.error or "").lower()
    if pid_log.exists():
        pid = int(pid_log.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert list(remote_base.iterdir()) == []


def test_parallel_remote_initializes_ssh_runner_and_work_dir_once(monkeypatch, tmp_path) -> None:
    import threading
    import time

    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")

    runner_inits = 0
    username_calls = 0
    count_lock = threading.Lock()
    barrier = threading.Barrier(4, timeout=3.0)

    class _FakeSSHRunner:
        user = None
        persistent_shell_enabled = False

        def __init__(self, **kwargs):
            nonlocal runner_inits
            time.sleep(0.02)
            with count_lock:
                runner_inits += 1
            self.kwargs = kwargs

    def fake_resolve_username(**_kwargs):
        nonlocal username_calls
        time.sleep(0.02)
        with count_lock:
            username_calls += 1
        return "designer"

    def fake_run_spectre_remote(**kwargs):
        barrier.wait()
        return _successful_run(kwargs["base_output_dir"] / "tb_amp.raw")

    monkeypatch.setattr(runner, "SSHRunner", _FakeSSHRunner)
    monkeypatch.setattr(runner, "resolve_remote_username", fake_resolve_username)
    monkeypatch.setattr(runner, "resolve_client_id", lambda _p: "client1")
    monkeypatch.setattr(runner, "_run_spectre_remote", fake_run_spectre_remote)
    monkeypatch.setattr(runner, "load_vb_env", lambda: None)

    sim = runner.SpectreSimulator(remote_host="compute-host", work_dir=tmp_path / "runs")
    results = sim.run_parallel([(netlist, {}) for _ in range(4)], max_workers=4)

    assert all(r.ok for r in results)
    assert runner_inits == 1
    assert username_calls == 1
    assert sim._remote_work_dir_set is True
    assert sim._ssh_runner.kwargs["persistent_shell"] is False

    # Subsequent calls must not re-run username discovery.
    more = sim.run_parallel([(netlist, {}) for _ in range(4)], max_workers=4)
    assert all(r.ok for r in more)
    assert username_calls == 1


def test_parallel_remote_does_not_serialize_on_persistent_shell_lock(monkeypatch, tmp_path) -> None:
    import threading
    from virtuoso_bridge.transport.ssh import CommandResult, SSHRunner

    netlist = tmp_path / "tb_amp.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")

    # Simulate an OpenSSH runner inherited from SSHClient (persistent_shell=True).
    persistent = SSHRunner(host="compute-host", user="designer", persistent_shell=True)
    persistent._persistent_shell_enabled = True

    def forbid_persistent(*_args, **_kwargs):
        raise AssertionError("Spectre remote execution must not use persistent shell _shell_lock")

    monkeypatch.setattr(SSHRunner, "_run_via_persistent_shell_with_retry", forbid_persistent)

    exec_barrier = threading.Barrier(2, timeout=3.0)

    def fake_upload_batch(self, _uploads, timeout=None):
        return CommandResult(0, "", "")

    def fake_download(self, _remote, local, recursive=False, timeout=None):
        if recursive:
            Path(local).mkdir(parents=True, exist_ok=True)
        return CommandResult(0, "", "")

    def fake_run_command_once(self, command, timeout=None, *, _budget=None):
        if "spectre.pid" in command:
            # Both workers must reach remote execution concurrently; serialization deadlocks here.
            exec_barrier.wait()
        return CommandResult(0, "", "")

    monkeypatch.setattr(SSHRunner, "upload_batch", fake_upload_batch)
    monkeypatch.setattr(SSHRunner, "download", fake_download)
    monkeypatch.setattr(SSHRunner, "_run_command_once", fake_run_command_once)
    monkeypatch.setattr(runner, "load_vb_env", lambda: None)
    monkeypatch.setattr(runner.CadenceEnvironment, "from_env", lambda *a: runner.CadenceEnvironment(""))

    sim = runner.SpectreSimulator(
        remote_host="compute-host",
        remote_work_dir="/tmp/spectre-runs",
        work_dir=tmp_path / "runs",
        ssh_runner=persistent,
        output_format=None,
    )
    results = sim.run_parallel([(netlist, {}), (netlist, {})], max_workers=2)

    assert all(r.ok for r in results), [r.errors for r in results]
    assert sim._ssh_runner is persistent
    assert persistent.persistent_shell_enabled is True


def test_spectre_remote_work_dir_expands_tilde_scratch_root(monkeypatch, tmp_path) -> None:
    from virtuoso_bridge.transport.ssh import CommandResult

    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/vb_scratch")
    monkeypatch.setenv("VB_CLIENT_ID", "client1")

    class _HomeRunner:
        user = "designer"

        def run_command(self, command: str, timeout=None) -> CommandResult:
            if "getent" in command:
                return CommandResult(0, "/home/remote\n", "")
            return CommandResult(1, "", "")

    sim = runner.SpectreSimulator(
        remote_host="compute-host",
        remote_user="designer",
        work_dir=tmp_path / "runs",
        ssh_runner=_HomeRunner(),
    )

    resolved = sim._resolve_remote_work_dir(sim._ssh_runner)

    assert resolved == (
        "/home/remote/vb_scratch/virtuoso_bridge_designer/client1/virtuoso_bridge_spectre"
    )
    assert not resolved.startswith("~")

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

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

    assert calls[0][0][:2] == ["csh", "-fc"]
    assert "source /eda/cadence.cshrc" in calls[0][0][2]


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

    try:
        results = simulator.run_parallel(
            [(netlist, {}), (netlist, {})],
            max_workers=2,
        )
    finally:
        simulator.shutdown()

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

    try:
        results = simulator.run_parallel(
            [(netlist, {}), (netlist, {})],
            max_workers=2,
        )
    finally:
        simulator.shutdown()

    assert all(result.ok for result in results)
    assert len(set(task_work_dirs)) == 2
    assert all(path.parent == work_dir for path in task_work_dirs)
    assert all(re.fullmatch(r"tb_amp__[0-9a-f]{8}", path.name) for path in task_work_dirs)

from __future__ import annotations

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

"""Regressions for Maestro/SKILL pitfalls seen driving IC23.10 headless.

Each test names the pitfall: silent result export, "done" runs with
failed points, options Maestro accepts but never netlists, outputs that
keep their old expression, point netlists made of relative symlinks,
errors inside a load()ed file, and background jobs started over SSH.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge.transport.ssh import CommandResult, SSHRunner
from virtuoso_bridge.virtuoso.basic.bridge import _load_il_skill
from virtuoso_bridge.virtuoso.maestro import (
    MaestroOps,
    MaestroPointsFailed,
    NoResults,
    add_output,
    check_points,
    copy_point_netlist,
    export_output_view,
    missing_netlist_options,
    run_and_wait,
    set_analysis,
)
from virtuoso_bridge.virtuoso.maestro import writer
from virtuoso_bridge.virtuoso.maestro.reader.runs import _classify_spectre_log


class _Client:
    def __init__(self, outputs=None) -> None:
        self.expressions: list[str] = []
        self._outputs = list(outputs or [])
        self.ssh_runner = None

    def execute_skill(self, expression: str, **kwargs):
        self.expressions.append(expression)
        output = self._outputs.pop(0) if self._outputs else "t"
        return SimpleNamespace(errors=[], output=output)


# -- maeExportOutputView writing nothing ------------------------------------

def test_no_results_names_the_reason_instead_of_a_bare_key_error() -> None:
    empty = NoResults("maeExportOutputView did not produce a CSV")
    assert empty == {} and not empty
    with pytest.raises(KeyError, match="did not produce a CSV"):
        empty["points"]


def test_export_output_view_fails_when_no_file_was_written() -> None:
    client = _Client()
    export_output_view(client, "/tmp/out.csv")
    skill = client.expressions[0]
    assert 'errset(maeExportOutputView(?view "Detail" ?fileName "/tmp/out.csv") t)' in skill
    assert 'unless(isFile("/tmp/out.csv") error(' in skill


# -- "done" with failed points ----------------------------------------------

def test_spectre_log_classification() -> None:
    assert _classify_spectre_log(["spectre completes with 0 errors, 2 warnings"]) == ("ok", [])
    status, errors = _classify_spectre_log(
        ["ERROR (SPECTRE-18): Segmentation fault.", "spectre terminated prematurely"])
    assert status == "failed" and errors[0].startswith("ERROR (SPECTRE-18)")
    assert _classify_spectre_log(["spectre completes with 2 errors"])[0] == "failed"
    assert _classify_spectre_log([])[0] == "incomplete"


def _results_tree(tmp_path: Path) -> Path:
    history = tmp_path / "cell" / "maestro" / "results" / "maestro" / "Interactive.3"
    for point, text in (("1", "spectre completes with 0 errors, 0 warnings\n"),
                        ("2", "ERROR (SPECTRE-19): internal error\n")):
        log = history / point / "TRAN" / "psf" / "spectre.out"
        log.parent.mkdir(parents=True)
        log.write_text("Notice from spectre\n" + text)
    return history


def test_check_points_reads_each_spectre_log(tmp_path) -> None:
    _results_tree(tmp_path)
    client = _Client([f'"{tmp_path}"'])
    points = check_points(client, '"Interactive.3"', lib="LIB", cell="cell")
    assert [(p["point"], p["status"]) for p in points] == [("1", "ok"), ("2", "failed")]
    assert points[1]["log"] == "2/TRAN/psf/spectre.out"


def test_run_and_wait_check_raises_on_failed_points(tmp_path, monkeypatch) -> None:
    _results_tree(tmp_path)
    monkeypatch.setattr(writer, "run_simulation", lambda *a, **k: '"Interactive.3"')
    monkeypatch.setattr(writer, "_wait_until_done", lambda *a, **k: "done")
    client = _Client(["t", f'"{tmp_path}"'])
    with pytest.raises(MaestroPointsFailed, match="1 of 2 points") as info:
        run_and_wait(client, session="s1", check=True, lib="LIB", cell="cell")
    assert info.value.history == "Interactive.3"


def test_run_and_wait_without_check_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(writer, "run_simulation", lambda *a, **k: '"Interactive.3"')
    monkeypatch.setattr(writer, "_wait_until_done", lambda *a, **k: "done")
    client = _Client()
    assert run_and_wait(client, session="s1") == ('"Interactive.3"', "done")
    assert not any("readPath" in e for e in client.expressions)


# -- options accepted but never netlisted ----------------------------------

NETLIST = (
    "simulatorOptions options reltol=1e-3\n"
    "tran tran stop=100n errpreset=conservative \\\n"
    "    write=\"spectre.ic\" annotate=status\n"
)


def test_missing_netlist_options_follows_continuations() -> None:
    assert missing_netlist_options(NETLIST, "tran", ["stop", "write", "noisefmax"]) == ["noisefmax"]
    assert missing_netlist_options(NETLIST, "ac", ["start"]) == ["start"]


def test_check_netlist_options_reads_the_generated_netlist(tmp_path) -> None:
    client = _Client()
    client.download_file = lambda remote, local: Path(local).write_text(NETLIST)
    missing = MaestroOps(client).check_netlist_options(
        "/run/input.scs", "tran", '(("stop" "100n") ("noisefmax" "10G"))')
    assert missing == ["noisefmax"]


def test_set_analysis_warns_about_transient_noise_options(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="virtuoso_bridge.virtuoso.maestro.writer"):
        set_analysis(_Client(), "TRAN", "tran", options='(("stop" "1u") ("noisefmax" "10G"))')
    assert "noisefmax" in caplog.text and "check_netlist_options" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="virtuoso_bridge.virtuoso.maestro.writer"):
        set_analysis(_Client(), "TRAN", "tran", options='(("stop" "1u"))')
    assert caplog.text == ""


# -- maeAddOutput keeping the old expression --------------------------------

def test_add_output_replace_deletes_the_old_output_first() -> None:
    client = _Client()
    add_output(client, "tdly", "TRAN", expr="delay()", session="s1", replace=True)
    assert client.expressions == [
        'progn(errset(maeDeleteOutput("tdly" "TRAN" ?session "s1") nil) '
        'maeAddOutput("tdly" "TRAN" ?expr "delay()" ?session "s1"))'
    ]
    client = _Client()
    add_output(client, "tdly", "TRAN", expr="delay()")
    assert client.expressions[0].startswith("maeAddOutput(")


# -- point netlists made of relative symlinks -------------------------------

def test_copy_point_netlist_follows_relative_symlinks(tmp_path) -> None:
    real = tmp_path / "psf" / "TRAN" / "netlist"
    real.mkdir(parents=True)
    (real / "input.scs").write_text("tran tran stop=1n\n")
    point = tmp_path / "1" / "TRAN"
    point.mkdir(parents=True)
    (point / "netlist").symlink_to(Path("../../psf/TRAN/netlist"))
    copy_point_netlist(_Client(), str(point), str(tmp_path / "copy"))
    copied = tmp_path / "copy" / "netlist"
    assert not copied.is_symlink()
    assert (copied / "input.scs").read_text() == "tran tran stop=1n\n"


# -- errors inside a load()ed file ------------------------------------------

def test_load_il_reports_the_error_raised_inside_the_file() -> None:
    skill = _load_il_skill("/tmp/my file.il")
    assert 'vbRun = errset(load("/tmp/my file.il") t)' in skill
    assert 'rexMatchp("[*]Error[*]" vbLine)' in skill
    assert skill.count("(") == skill.count(")")


# -- background jobs over SSH -----------------------------------------------

def test_run_detached_runs_under_sh_and_survives_the_caller(tmp_path) -> None:
    runner = object.__new__(SSHRunner)

    def run_command(command, timeout=None):
        done = subprocess.run(["sh"], input=command, capture_output=True,
                              text=True, timeout=10)
        return CommandResult(done.returncode, done.stdout, done.stderr)

    runner.run_command = run_command
    log = tmp_path / "job.log"
    pid = runner.run_detached("echo out && echo err >&2", str(log))
    assert pid > 0
    for _ in range(50):
        if "err" in (log.read_text() if log.exists() else ""):
            break
        subprocess.run(["sleep", "0.1"])
    assert log.read_text().split() == ["out", "err"]

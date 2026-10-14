"""Offline checks of the live suite's Maestro scratch cleanup: which directories
it picks and how it removes them. Deleting the wrong one would hit user data."""

from __future__ import annotations

import os
import subprocess

import pytest

from tests import test_bridge_live as live
from virtuoso_bridge.transport.ssh import CommandResult

LIB = "vbtest_lib"
CELL = f"{live.PREFIX}tb_rc_{live.RUN}"
LIB_PATH = "/home/designer/libs/vbtest_lib"
PROJECT = "/home/designer/simulation"

_needs_posix_sh = pytest.mark.skipif(os.name == "nt", reason="runs the generated POSIX sh")


def _dirs(run_dirs, **overrides):
    kwargs = dict(lib=LIB, cell=CELL, view="maestro", lib_path=LIB_PATH, project_dir=PROJECT)
    kwargs.update(overrides)
    return live._maestro_scratch_dirs(run_dirs, **kwargs)


def test_default_project_directory_is_always_a_candidate():
    assert _dirs([]) == [f"{PROJECT}/{LIB}/{CELL}"]


def test_a_configured_results_location_is_taken_from_the_reported_run_dir():
    reported = [f"/scratch/res/{LIB}/{CELL}/maestro/results/maestro/.tmpADEDir_designer/AC/netlist"]
    assert _dirs(reported) == [f"{PROJECT}/{LIB}/{CELL}", f"/scratch/res/{LIB}/{CELL}"]


def test_paths_inside_or_around_the_library_are_never_returned():
    inside = f"{LIB_PATH}/{CELL}/maestro/results/maestro/Interactive.0/psf"
    assert _dirs([f"/home/designer/libs/{LIB}/{CELL}/maestro/results/maestro/Interactive.0"]) == [
        f"{PROJECT}/{LIB}/{CELL}"]
    assert _dirs([inside]) == [f"{PROJECT}/{LIB}/{CELL}"]
    # A library that sits inside the candidate would be deleted with it.
    assert _dirs([], lib_path=f"{PROJECT}/{LIB}/{CELL}/lib") == []


def test_unrelated_or_relative_paths_add_nothing():
    assert _dirs(["/tmp/other/run", "simulation/x", f"{LIB}/{CELL}/maestro/results/maestro/x"]) == [
        f"{PROJECT}/{LIB}/{CELL}"]
    assert _dirs([], project_dir="simulation") == []


def test_a_relative_or_missing_library_path_is_refused():
    for lib_path in ("", "libs/vbtest_lib"):
        with pytest.raises(ValueError, match="library path must be absolute"):
            _dirs([], lib_path=lib_path)


class _ShellRunner:
    """Runs the generated command with the local sh, as the SSH runner would."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.commands.append(command)
        proc = subprocess.run(["sh", "-s"], input=command, capture_output=True, text=True, timeout=30)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class _Client:
    def __init__(self, runner) -> None:
        self.ssh_runner = runner


@_needs_posix_sh
def test_remote_removal_deletes_the_cell_dir_and_an_emptied_lib_dir(tmp_path):
    target = tmp_path / LIB / CELL
    (target / "maestro" / "results").mkdir(parents=True)
    (target / "maestro" / "results" / "data.psf").write_text("x\n", encoding="utf-8")

    live._remove_run_scratch(_Client(_ShellRunner()), str(target), CELL)

    assert not target.exists() and not (tmp_path / LIB).exists()


@_needs_posix_sh
def test_remote_removal_keeps_a_lib_dir_that_still_holds_other_cells(tmp_path):
    target = tmp_path / LIB / CELL
    target.mkdir(parents=True)
    (tmp_path / LIB / "users_own_cell").mkdir()

    live._remove_run_scratch(_Client(_ShellRunner()), str(target), CELL)

    assert not target.exists() and (tmp_path / LIB / "users_own_cell").is_dir()


@_needs_posix_sh
def test_remote_removal_refuses_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "keep.txt").write_text("keep\n", encoding="utf-8")
    (tmp_path / LIB).mkdir()
    link = tmp_path / LIB / CELL
    link.symlink_to(real)

    with pytest.raises(AssertionError, match="refusing symlink"):
        live._remove_run_scratch(_Client(_ShellRunner()), str(link), CELL)

    assert link.is_symlink() and (real / "keep.txt").is_file()


@pytest.mark.parametrize("name", ["users_own_cell", f"{live.PREFIX}tb_rc_other0"])
def test_removal_refuses_a_directory_this_run_did_not_create(tmp_path, name):
    runner = _ShellRunner()
    with pytest.raises(AssertionError):
        live._remove_run_scratch(_Client(runner), str(tmp_path / LIB / name), name)
    assert runner.commands == []


def test_local_removal_without_ssh(tmp_path):
    target = tmp_path / LIB / CELL
    target.mkdir(parents=True)
    (target / "f").write_text("x", encoding="utf-8")

    live._remove_run_scratch(_Client(None), str(target), CELL)

    assert not target.exists() and not (tmp_path / LIB).exists()


def test_removing_a_directory_that_is_already_gone_is_fine(tmp_path):
    live._remove_run_scratch(_Client(None), str(tmp_path / LIB / CELL), CELL)

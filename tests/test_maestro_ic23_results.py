"""IC23.1 result reads: per-corner Detail CSV and runs outside the library.

Seen live: ``read_results`` returned 0 points for a corner run because the
Detail CSV puts one column per corner, and the export was narrowed to the
setup's first test, which had not run in that history. ``check_points``
returned ``[]`` because Maestro writes point directories under the ADE
project directory (``asimenv.startup projectDir``), not the library.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.maestro import check_points
from virtuoso_bridge.virtuoso.maestro.reader.runs import _parse_detail_csv, read_results

IC23_CSV = (
    ",Parameter,,,,,,pn_tt_0p8,pn_ss_0p9\n"
    ",design_wrapper.lib.scs,,,,,,tt_pre,ss_pre\n"
    ",vdd,,,,,,800e-3,900e-3\n"
    "\n\n"
    "Test,Output,Spec,Weight,Pass/Fail,Min,Max,pn_tt_0p8,pn_ss_0p9\n"
    "PNOISE,/CMPCK,,,,,,,\n"
    "PNOISE,noise,< 1m,,pass,365e-6,456e-6,456e-6,365e-6\n"
    "PNOISE,FoM1,,,,5.591,7.043,7.043,5.591\n"
)


def test_parse_per_corner_columns() -> None:
    out = _parse_detail_csv(IC23_CSV, history="Interactive.5")
    assert out["tests"] == ["PNOISE"]
    assert [p["corner"] for p in out["points"]] == ["pn_tt_0p8", "pn_ss_0p9"]
    tt, ss = out["points"]
    assert tt["parameters"] == {"design_wrapper.lib.scs": "tt_pre", "vdd": "800e-3"}
    assert ss["parameters"]["vdd"] == "900e-3"
    assert tt["outputs"]["noise"] == {"value": "456e-6", "spec": "< 1m",
                                      "weight": "", "pass_fail": "pass"}
    assert ss["outputs"]["FoM1"]["value"] == "5.591"
    assert tt["outputs"]["/CMPCK"]["value"] == ""
    assert len(out["outputs"]) == 6


class _Client:
    def __init__(self, responses, csv_text="") -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.ssh_runner = None
        self._csv = csv_text

    def execute_skill(self, code, timeout=None):
        self.calls.append(code)
        out = self._responses.pop(0) if self._responses else "nil"
        return SimpleNamespace(output=out, errors=[])

    def download_file(self, remote, local):
        Path(local).write_text(self._csv)


def test_read_results_exports_every_test() -> None:
    client = _Client(['("DELAY" "PNOISE")', "t", "t", "nil", "nil"], IC23_CSV)
    out = read_results(client, "s1", lib="L", cell="c", history="Interactive.5")
    export = next(c for c in client.calls if "maeExportOutputView" in c)
    assert "?testName" not in export and '?session "s1"' in export
    assert len(out["points"]) == 2


def test_latest_history_opens_in_the_given_session_and_any_test(tmp_path) -> None:
    client = _Client(['("DELAY" "PNOISE")', '("Interactive.4" "Interactive.5.rdb" "Interactive.5")',
                      '("noise")', "t", "t", "nil", "nil"], IC23_CSV)
    out = read_results(client, "s1", lib="L", cell="c")
    scan = next(c for c in client.calls if "maeOpenResults" in c)
    assert 'maeOpenResults(?history "Interactive.5" ?session "s1")' in scan
    assert 'maeGetSetup(?typeName "tests" ?session "s1")' in scan
    assert out["history"] == "Interactive.5"


def _log(root: Path, point: str, text: str) -> None:
    log = root / point / "PNOISE" / "psf" / "spectre.out"
    log.parent.mkdir(parents=True)
    log.write_text(text)


def test_check_points_finds_runs_under_the_project_dir(tmp_path) -> None:
    lib = tmp_path / "lib"
    (lib / "cell" / "maestro" / "results" / "maestro").mkdir(parents=True)
    project = tmp_path / "sim"
    run = project / "LIB" / "cell" / "maestro" / "results" / "maestro" / "Interactive.5"
    _log(run, "1", "spectre completes with 0 errors\n")
    _log(run, "2", "Internal error found in spectre at time = 49 ns\n")
    client = _Client([f'"{lib}"', f'"{project}"'])
    points = check_points(client, "Interactive.5", lib="LIB", cell="cell")
    assert [(p["point"], p["status"]) for p in points] == [("1", "ok"), ("2", "failed")]


def test_check_points_uses_the_path_recorded_in_the_rdb(tmp_path) -> None:
    lib = tmp_path / "lib"
    results = lib / "cell" / "maestro" / "results" / "maestro"
    results.mkdir(parents=True)
    run = tmp_path / "elsewhere" / "LIB" / "cell" / "maestro" / "results" / "maestro" / "Interactive.2"
    _log(run, "1", "spectre completes with 0 errors\n")
    (results / "Interactive.2.rdb").write_bytes(b"SQLite\x00_ROWID_=1" + str(run).encode() + b"$AXL")
    client = _Client([f'"{lib}"', "nil"])
    assert [p["status"] for p in check_points(client, "Interactive.2", lib="LIB", cell="cell")] == ["ok"]


def test_check_points_without_logs_raises_instead_of_empty(tmp_path) -> None:
    lib = tmp_path / "lib"
    (lib / "cell").mkdir(parents=True)
    client = _Client([f'"{lib}"', "nil"])
    with pytest.raises(RuntimeError, match="no spectre.out"):
        check_points(client, "Interactive.9", lib="LIB", cell="cell")

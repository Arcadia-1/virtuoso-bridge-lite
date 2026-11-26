"""Offline regression tests for check_spectre_netlist.py and track_traffic.py (#346)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "skills" / "netlist" / "scripts" / "check_spectre_netlist.py"
TRAFFIC_PATH = ROOT / "scripts" / "track_traffic.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_module("vb_test_check_spectre_netlist", CHECKER_PATH)
traffic = _load_module("vb_test_track_traffic", TRAFFIC_PATH)


# ---------------------------------------------------------------------------
# check_spectre_netlist.py
# ---------------------------------------------------------------------------

def test_inline_subckt_passes_without_structure_error(tmp_path):
    deck = tmp_path / "inline_dut.scs"
    deck.write_text(
        """\
// Valid Spectre inline subcircuit
inline subckt my_nmos (d g s b)
  my_nmos (d g s b) nmos_mod l=180n w=1u
ends my_nmos
""",
        encoding="utf-8",
    )

    for mode in ("any", "dut"):
        issues = checker.check_file(deck, mode, set(checker.DEFAULT_MOS_KEEP))
        assert issues == []

    proc = subprocess.run(
        [sys.executable, str(CHECKER_PATH), str(deck), "--mode", "dut"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_unmatched_ends_still_errors(tmp_path):
    deck = tmp_path / "bad.scs"
    deck.write_text("ends orphan_cell\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(CHECKER_PATH), str(deck)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 1
    assert "without matching `subckt`" in proc.stdout


def test_named_spectre_directives_detected_without_false_flagging_instances(tmp_path):
    deck = tmp_path / "named_run.scs"
    deck.write_text(
        """\
simulator lang=spectre
global 0 dc
parameters dc=1 ac=0.5
subckt inv (in out vdd vss)
  mp (out in vdd vdd) pch l=180n w=2u
  mn (out in vss vss) nch l=180n w=1u
  x_dc (in out) dc
  tran (in out) inv_buf
ends inv

Vdd (vdd 0) vsource dc=1.8
I_dut (vin vout vdd 0) inv
dcOp dc
dcOpInfo info what=oppoint where=rawfile
saveOptions options save=allpub
tran1 tran stop=10n
ac1 ac start=1M stop=10G
pss1 pss fund=1G harms=10
pnoise1 pnoise start=0 stop=500M
stb1 stb probe=IPRB0 start=1 stop=10G
sp1 sp ports=[P1 P2] start=1G stop=10G
hb1 hb tones=[1G] maxharms=[5]
env1 envlp stop=1u
mc1 montecarlo numruns=10
a1 assert expr="V(vout) > 0"
c1 checklimit check_all_asserts=yes
n1 noise start=1 stop=1G
""",
        encoding="utf-8",
    )

    dut_issues = checker.check_file(deck, "dut", set(checker.DEFAULT_MOS_KEEP))
    run_warnings = [
        issue.subject
        for issue in dut_issues
        if issue.category == "dut-boundary"
        and "analyses, saves, probes, or simulator options" in issue.message
    ]
    assert run_warnings == [
        "dcOp",
        "dcOpInfo",
        "saveOptions",
        "tran1",
        "ac1",
        "pss1",
        "pnoise1",
        "stb1",
        "sp1",
        "hb1",
        "env1",
        "mc1",
        "a1",
        "c1",
        "n1",
    ]

    # Regular instances and non-run statements must NOT be flagged as run directives.
    for non_directive in ("global", "parameters", "mp", "mn", "x_dc", "tran", "Vdd", "I_dut"):
        assert non_directive not in run_warnings

    # In run mode, all named analyses/options are valid (zero errors and zero dut-boundary warnings).
    run_issues = checker.check_file(deck, "run", set(checker.DEFAULT_MOS_KEEP))
    assert not any(issue.severity == "error" for issue in run_issues)
    assert not any(issue.category == "dut-boundary" for issue in run_issues)


def test_repo_example_netlist_recognized_in_dut_and_run_modes():
    sample = ROOT / "examples" / "02_spectre" / "assets" / "cap_dc_ac" / "tb_cap_dc_ac.scs"
    dut_issues = checker.check_file(sample, "dut", set(checker.DEFAULT_MOS_KEEP))
    subjects = {
        issue.subject
        for issue in dut_issues
        if "analyses, saves, probes, or simulator options" in issue.message
    }
    assert {"simulatorOptions", "dcOp", "dcOpInfo", "ac", "save", "saveOptions"} <= subjects

    run_issues = checker.check_file(sample, "run", set(checker.DEFAULT_MOS_KEEP))
    assert not any(issue.severity == "error" for issue in run_issues)


# ---------------------------------------------------------------------------
# scripts/track_traffic.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_content", ["{not valid json", "", "[]", '"hello"', "42", "null"])
def test_load_history_rejects_corrupt_or_non_dict_json(tmp_path, bad_content):
    path = tmp_path / "clones.json"
    path.write_text(bad_content, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid"):
        traffic._load_history(path)


@pytest.mark.parametrize("corrupt_name", ["clones.json", "views.json"])
def test_track_traffic_main_refuses_to_overwrite_on_corrupt_history(
    tmp_path, monkeypatch, capsys, corrupt_name
):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    clones_file = stats_dir / "clones.json"
    views_file = stats_dir / "views.json"
    readme_file = stats_dir / "README.md"

    valid_json = json.dumps({"2026-01-01": {"count": 10, "uniques": 5}}) + "\n"
    corrupt_text = "{truncated\n"
    readme_text = "# Existing README\n"

    clones_file.write_text(corrupt_text if corrupt_name == "clones.json" else valid_json, encoding="utf-8")
    views_file.write_text(corrupt_text if corrupt_name == "views.json" else valid_json, encoding="utf-8")
    readme_file.write_text(readme_text, encoding="utf-8")

    monkeypatch.setattr(traffic, "STATS_DIR", stats_dir)
    monkeypatch.setenv("GH_TOKEN", "dummy-token")
    monkeypatch.setenv("OWNER", "Arcadia-1")
    monkeypatch.setenv("REPO", "virtuoso-bridge-lite")
    monkeypatch.setattr(traffic, "_get", lambda *a, **kw: pytest.fail("_get must not run when history is corrupt"))

    rc = traffic.main()
    captured = capsys.readouterr()

    assert rc != 0
    assert corrupt_name in captured.err
    # Every existing archive artifact remains completely untouched.
    assert clones_file.read_text(encoding="utf-8") == (
        corrupt_text if corrupt_name == "clones.json" else valid_json
    )
    assert views_file.read_text(encoding="utf-8") == (
        corrupt_text if corrupt_name == "views.json" else valid_json
    )
    assert readme_file.read_text(encoding="utf-8") == readme_text


def test_track_traffic_main_preserves_old_history_on_valid_merge(tmp_path, monkeypatch):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    clones_file = stats_dir / "clones.json"
    views_file = stats_dir / "views.json"
    clones_file.write_text(json.dumps({"2026-01-01": {"count": 10, "uniques": 5}}), encoding="utf-8")
    views_file.write_text(json.dumps({"2026-01-01": {"count": 20, "uniques": 8}}), encoding="utf-8")

    monkeypatch.setattr(traffic, "STATS_DIR", stats_dir)
    monkeypatch.setenv("GH_TOKEN", "dummy-token")
    monkeypatch.setenv("OWNER", "Arcadia-1")
    monkeypatch.setenv("REPO", "virtuoso-bridge-lite")

    def fake_get(url, token):
        if url.endswith("/clones"):
            return {"clones": [{"timestamp": "2026-02-01T00:00:00Z", "count": 3, "uniques": 2}]}
        return {"views": [{"timestamp": "2026-02-01T00:00:00Z", "count": 7, "uniques": 4}]}

    monkeypatch.setattr(traffic, "_get", fake_get)

    rc = traffic.main()
    assert rc == 0
    assert json.loads(clones_file.read_text(encoding="utf-8")) == {
        "2026-01-01": {"count": 10, "uniques": 5},
        "2026-02-01": {"count": 3, "uniques": 2},
    }
    assert json.loads(views_file.read_text(encoding="utf-8")) == {
        "2026-01-01": {"count": 20, "uniques": 8},
        "2026-02-01": {"count": 7, "uniques": 4},
    }

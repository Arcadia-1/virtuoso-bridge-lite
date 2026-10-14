"""A sent key is not proof of dismissal (issue #244)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from virtuoso_bridge import cli
from virtuoso_bridge.virtuoso import x11


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[1] / "src/virtuoso_bridge/resources/x11_dismiss_dialog.py"
    spec = importlib.util.spec_from_file_location("x11_dismiss_outcome", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("result", [
    {"dismissed": "0x10", "action": "enter", "still_mapped": True},
    {"status": "requested", "window_id": "0x10", "action": "enter", "action_sent": True},
    {"dismissed": "0x10", "action": "enter"},  # old helper: no closure evidence
])
def test_cli_never_confirms_an_unverified_dismissal(monkeypatch, capsys, result):
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (None, "fixture"))
    monkeypatch.setattr(x11, "dismiss_window", lambda *args, **kwargs: [dict(result)])
    assert cli.main(["dismiss-window", "0x10", "--display", ":7"]) != 0
    output = capsys.readouterr().out
    assert "Dismissed:" not in output
    assert "Requested:" in output


@pytest.mark.parametrize("state,expected", [
    ({"exists": True, "mapped": True}, "requested"),
    ({"exists": True, "mapped": False}, "closed"),
    ({"exists": False, "mapped": False}, "closed"),
])
def test_exact_window_state_controls_confirmation(helper, monkeypatch, state, expected):
    monkeypatch.setattr(helper.time, "sleep", lambda seconds: None)
    queried = []
    monkeypatch.setattr(helper, "_open_x11_inventory", lambda *args: SimpleNamespace(
        window_state=lambda xid: queried.append(xid) or state, close=lambda: None))
    result = helper._verify_dismissal({"dismissed": "0x10", "child": "0x10",
                                      "action": "enter", "display": ":7"})
    assert queried == [0x10]
    assert result["status"] == expected
    assert result["action_sent"] is True
    assert result["closure_confirmed"] is (expected == "closed")
    assert ("dismissed" in result) is (expected == "closed")


def test_unavailable_display_never_confirms_closure(helper, monkeypatch):
    monkeypatch.setattr(helper.time, "sleep", lambda seconds: None)

    def unavailable(*args):
        raise helper._InspectionFailure("cannot open display")

    monkeypatch.setattr(helper, "_open_x11_inventory", unavailable)
    result = helper._verify_dismissal({"dismissed": "0x10", "display": ":7"})
    assert result["status"] == "requested"
    assert result["still_mapped"] is None
    assert result["closure_confirmed"] is False
    assert "dismissed" not in result


def test_cli_confirms_only_verified_closed_result(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (None, "fixture"))
    monkeypatch.setattr(x11, "dismiss_window", lambda *args, **kwargs: [
        {"status": "closed", "closure_confirmed": True, "still_mapped": False, "dismissed": "0x10"}])
    assert cli.main(["dismiss-window", "0x10", "--display", ":7"]) == 0
    assert "Dismissed:" in capsys.readouterr().out


@pytest.mark.parametrize("closed", [False, True])
def test_registered_cli_entrypoint_preserves_outcome_exit_code(tmp_path, closed):
    # Only the external action seam is replaced; run the installed console script.
    result = {"status": "closed" if closed else "requested", "window_id": "0x10",
              "action_sent": True, "closure_confirmed": closed, "still_mapped": not closed}
    (tmp_path / "sitecustomize.py").write_text(
        "from virtuoso_bridge import cli\n"
        "from virtuoso_bridge.virtuoso import x11\n"
        "cli._load_cli_env = lambda: None\n"
        "cli._make_ssh_runner = lambda: (None, 'fixture')\n"
        f"x11.dismiss_window = lambda *a, **kw: [{result!r}]\n")
    env = dict(os.environ, PYTHONPATH=str(tmp_path) + os.pathsep
               + str(Path(__file__).resolve().parents[1] / "src"))
    script = Path(sys.executable).with_name("virtuoso-bridge.exe" if os.name == "nt" else "virtuoso-bridge")
    completed = subprocess.run([str(script), "dismiss-window", "0x10", "--display", ":7"],
                               env=env, capture_output=True, text=True, timeout=10)
    assert completed.returncode == (0 if closed else 1), completed.stderr
    assert ("Dismissed:" in completed.stdout) is closed

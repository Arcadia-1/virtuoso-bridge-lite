"""A sent key is not proof of dismissal (issue #244)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge import cli
from virtuoso_bridge.virtuoso import x11


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
def test_exact_window_state_controls_confirmation(monkeypatch, state, expected):
    path = Path(__file__).resolve().parents[1] / "src/virtuoso_bridge/resources/x11_dismiss_dialog.py"
    spec = importlib.util.spec_from_file_location("x11_dismiss_outcome", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
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


def test_unavailable_display_never_confirms_closure(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "src/virtuoso_bridge/resources/x11_dismiss_dialog.py"
    spec = importlib.util.spec_from_file_location("x11_unknown_outcome", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
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

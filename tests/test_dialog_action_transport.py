"""Bounded shell protocol and helper-version isolation, with no remote calls."""

import base64
import json
import shlex
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso import x11


class Runner:
    def __init__(self):
        self.commands = []
        self.uploads = []
        self.response = '{"status":"prepared"}'
        self.returncode = 0

    def run_command(self, command, timeout=None):
        self.commands.append((command, timeout))
        if command.startswith("mkdir"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "CMD:python" in command:
            return SimpleNamespace(returncode=0, stdout="CMD:python\n", stderr="")
        return SimpleNamespace(returncode=self.returncode, stdout=self.response, stderr="")

    def upload(self, local, remote, timeout=None):
        self.uploads.append((local, remote, timeout))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def setup(monkeypatch, tmp_path):
    old = tmp_path / "x11_dismiss_dialog.py"
    new = tmp_path / "x11_dialog_close.py"
    old.write_text("# read-only helper\n", encoding="ascii")
    new.write_text("# action helper\n", encoding="ascii")
    monkeypatch.setattr(x11, "_HELPER_SCRIPT", old)
    monkeypatch.setattr(x11, "default_virtuoso_bridge_dir", lambda *a: "/scratch/space name")
    monkeypatch.setattr(x11, "resolve_client_id", lambda *a: "scope")
    return Runner(), new


def test_versioned_helper_is_cached_and_payload_is_quoted(monkeypatch, tmp_path):
    runner, _ = setup(monkeypatch, tmp_path)
    payload = {"op": "prepare", "title": "Title ; $(never execute)", "pid": 42}
    x11.dialog_close_exchange(runner, "user", payload, timeout=30)
    x11.dialog_close_exchange(runner, "user", payload, timeout=30)
    assert len(runner.uploads) == 2
    command = runner.commands[-1][0]
    parts = shlex.split(command)
    decoded = json.loads(base64.b64decode(parts[2]))
    assert decoded["title"] == payload["title"]
    assert "never execute" not in command
    assert 0 < decoded["timeout"] <= 30
    assert all(0 < budget <= 30 for _, budget in runner.commands)


def test_changed_source_does_not_reuse_helper_version(monkeypatch, tmp_path):
    runner, source = setup(monkeypatch, tmp_path)
    x11.dialog_close_exchange(runner, "user", {"op": "prepare"}, timeout=30)
    old_path = runner.uploads[-1][1]
    source.write_text("# revised action helper\n", encoding="ascii")
    x11.dialog_close_exchange(runner, "user", {"op": "prepare"}, timeout=30)
    assert runner.uploads[-1][1] != old_path and len(runner.uploads) == 4


def test_oversized_input_never_executes_helper(monkeypatch, tmp_path):
    runner, _ = setup(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="payload"):
        x11.dialog_close_exchange(runner, "user", {"data": "x" * 16000}, timeout=30)
    assert not any(command.startswith("printf") for command, _ in runner.commands)


@pytest.mark.parametrize("reply", ["not json", "[]", "x" * (8 * 1024 * 1024 + 1)],
                         ids=["invalid-json", "not-object", "oversized"])
def test_malformed_or_oversized_reply_refused(monkeypatch, tmp_path, reply):
    runner, _ = setup(monkeypatch, tmp_path)
    runner.response = reply
    with pytest.raises((ValueError, RuntimeError)):
        x11.dialog_close_exchange(runner, "user", {"op": "prepare"}, timeout=30)


def test_upload_failure_never_runs_action(monkeypatch, tmp_path):
    runner, _ = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "upload", lambda *a, **k: SimpleNamespace(returncode=1, stderr="failed"))
    with pytest.raises(RuntimeError):
        x11.dialog_close_exchange(runner, "user", {"op": "close"}, timeout=30)
    assert not any(command.startswith("printf") for command, _ in runner.commands)


def test_definite_helper_refusal_preserves_not_started(monkeypatch, tmp_path):
    runner, _ = setup(monkeypatch, tmp_path)
    runner.returncode = 2
    runner.response = '{"status":"not_started","action_sent":false,"diagnostic":"pixels changed"}'
    answer = x11.dialog_close_exchange(runner, "user", {"op": "close"}, timeout=30)
    assert answer["status"] == "not_started" and answer["action_sent"] is False


@pytest.mark.parametrize("code,reply", [(1, '{"status":"not_started","action_sent":false}'),
                                      (2, '{"status":"requested","action_sent":true}')])
def test_nonzero_unknown_delivery_is_not_accepted(monkeypatch, tmp_path, code, reply):
    runner, _ = setup(monkeypatch, tmp_path)
    runner.returncode = code
    runner.response = reply
    with pytest.raises(RuntimeError):
        x11.dialog_close_exchange(runner, "user", {"op": "close"}, timeout=30)

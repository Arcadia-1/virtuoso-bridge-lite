"""Tests for ``virtuoso-bridge load`` CLI command.

After the issue follow-up:
  * ``cli_load`` delegates to :meth:`VirtuosoClient.load_il` so SKILL
    error messages keep the original ``.il`` file path + line number
    (no ``/tmp/vb_eval_*.il`` wrapper indirection).
  * Output is the full ``VirtuosoResult`` as JSON on stdout.
  * Exit codes: 0 on SUCCESS, 1 on SKILL-side error, 2 on missing
    local file.

Daemon-dependent paths (real Virtuoso connection) stay out of unit
tests; we monkey-patch ``VirtuosoClient.from_env`` to inject canned
results and assert the CLI's exit code + JSON shape.
"""
from __future__ import annotations

import json

import virtuoso_bridge
from virtuoso_bridge.cli import main
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult


class _FakeClient:
    def __init__(self, result: VirtuosoResult) -> None:
        self._result = result

    def load_il(self, path, timeout=None):
        return self._result


def _patch_client(monkeypatch, result: VirtuosoResult) -> None:
    """Replace ``VirtuosoClient.from_env`` with a stub yielding *result*.

    Also silences ``_load_cli_env`` so .env auto-discovery doesn't
    print to stdout (which would corrupt the JSON we capture).
    """
    fake = _FakeClient(result)

    class _FakeVirtuosoClient:
        @classmethod
        def from_env(cls, profile=None):
            return fake

    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FakeVirtuosoClient)
    monkeypatch.setattr("virtuoso_bridge.cli._load_cli_env", lambda: None)


def test_load_missing_file_returns_2(tmp_path, capsys):
    rc = main(["load", str(tmp_path / "nonexistent.il")])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err.lower()
    # Short-circuits before _load_cli_env(), so no env chatter on stdout.
    assert "using .env" not in captured.out


def test_load_success_emits_json_and_exits_0(tmp_path, capsys, monkeypatch):
    f = tmp_path / "ok.il"
    f.write_text('printf("hi\\n")\n')
    fake_result = VirtuosoResult(
        status=ExecutionStatus.SUCCESS,
        output="t",
        errors=[],
        warnings=[],
        execution_time=0.05,
        metadata={"uploaded": False, "skill_command": f'load("{f}")'},
    )
    _patch_client(monkeypatch, fake_result)

    rc = main(["load", str(f)])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "success"
    assert parsed["errors"] == []
    assert parsed["metadata"]["skill_command"].startswith("load(")


def test_load_skill_error_emits_json_and_exits_1(tmp_path, capsys, monkeypatch):
    f = tmp_path / "bad.il"
    f.write_text('printf(undef)\n')
    err_msg = (
        f'("load" 0 t nil ("*Error* load: error while loading file - '
        f'\\"{f}\\" at line 1"))'
    )
    fake_result = VirtuosoResult(
        status=ExecutionStatus.ERROR,
        output="",
        errors=[err_msg],
        warnings=[],
        execution_time=0.02,
        metadata={"uploaded": False, "skill_command": f'load("{f}")'},
    )
    _patch_client(monkeypatch, fake_result)

    rc = main(["load", str(f)])
    assert rc == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "error"
    assert len(parsed["errors"]) == 1
    # The error message must reference the original file path, not a
    # /tmp/vb_eval_xxx.il wrapper -- that's the whole point of using
    # load_il instead of execute_skill(file_content).
    assert str(f) in parsed["errors"][0]
    assert "/tmp/vb_eval_" not in parsed["errors"][0]


def test_load_quiet_suppresses_json_keeps_exit_code(tmp_path, capsys, monkeypatch):
    f = tmp_path / "ok.il"
    f.write_text("1\n")
    fake_result = VirtuosoResult(
        status=ExecutionStatus.SUCCESS,
        output="1",
        execution_time=0.01,
    )
    _patch_client(monkeypatch, fake_result)

    rc = main(["load", str(f), "--quiet"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_load_quiet_skill_error_still_returns_1(tmp_path, capsys, monkeypatch):
    f = tmp_path / "bad.il"
    f.write_text("1\n")
    fake_result = VirtuosoResult(
        status=ExecutionStatus.ERROR,
        errors=["something went wrong"],
    )
    _patch_client(monkeypatch, fake_result)

    rc = main(["load", str(f), "--quiet"])
    assert rc == 1
    assert capsys.readouterr().out == ""

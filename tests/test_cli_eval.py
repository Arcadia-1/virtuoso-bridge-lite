"""Tests for ``virtuoso-bridge eval`` CLI command.

``cli_eval`` is the one-liner companion to ``cli_load``: it forwards
an inline SKILL expression (from argv or stdin) to
:meth:`VirtuosoClient.execute_skill` and emits the same JSON shape as
``load`` so downstream consumers don't have to branch on the source
command.

Daemon-dependent paths (real Virtuoso connection) stay out of unit
tests; we monkey-patch ``VirtuosoClient.from_env`` to inject canned
results and assert exit code + JSON shape, plus the input-validation
exits (0/1/2) that don't need a daemon.
"""
from __future__ import annotations

import io
import json

import virtuoso_bridge
from virtuoso_bridge.cli import main
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult


class _FakeClient:
    def __init__(self, result: VirtuosoResult) -> None:
        self._result = result
        self.last_skill: str | None = None
        self.last_timeout: int | None = None
        self.connection_timeout: int | None = None

    def execute_skill(self, skill_code: str, timeout=None):
        self.last_skill = skill_code
        self.last_timeout = timeout
        return self._result


def _patch_client(monkeypatch, result: VirtuosoResult) -> _FakeClient:
    fake = _FakeClient(result)

    class _FakeVirtuosoClient:
        @classmethod
        def from_env(cls, profile=None, timeout=30):
            fake.connection_timeout = timeout
            return fake

    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FakeVirtuosoClient)
    monkeypatch.setattr("virtuoso_bridge.cli._load_cli_env", lambda: None)
    return fake


def test_eval_no_skill_returns_2(capsys):
    rc = main(["eval"])
    assert rc == 2
    assert "empty" in capsys.readouterr().err.lower()


def test_eval_empty_string_returns_2(capsys):
    rc = main(["eval", "   "])
    assert rc == 2
    assert "empty" in capsys.readouterr().err.lower()


def test_eval_argv_and_stdin_conflict_returns_2(capsys):
    rc = main(["eval", "1+1", "--stdin"])
    assert rc == 2
    assert "argv" in capsys.readouterr().err.lower()


def test_eval_argv_success_emits_json_and_exits_0(capsys, monkeypatch):
    fake_result = VirtuosoResult(
        status=ExecutionStatus.SUCCESS,
        output="2",
        execution_time=0.01,
    )
    fake = _patch_client(monkeypatch, fake_result)

    rc = main(["eval", "1+1"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "success"
    assert parsed["output"] == "2"
    assert "1+1" in fake.last_skill


def test_eval_stdin_success(capsys, monkeypatch):
    fake_result = VirtuosoResult(
        status=ExecutionStatus.SUCCESS,
        output="t",
        execution_time=0.01,
    )
    fake = _patch_client(monkeypatch, fake_result)
    monkeypatch.setattr("sys.stdin", io.StringIO('printf("hi\\n")\n'))

    rc = main(["eval", "--stdin"])
    assert rc == 0
    assert 'printf("hi\\n")' in fake.last_skill


def test_eval_skill_error_emits_json_and_exits_1(capsys, monkeypatch):
    fake_result = VirtuosoResult(
        status=ExecutionStatus.ERROR,
        errors=["*Error* eval: undefined variable foo"],
        execution_time=0.01,
    )
    _patch_client(monkeypatch, fake_result)

    rc = main(["eval", "foo"])
    assert rc == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "error"
    assert "undefined" in parsed["errors"][0]


def test_eval_quiet_suppresses_json(capsys, monkeypatch):
    fake_result = VirtuosoResult(
        status=ExecutionStatus.SUCCESS,
        output="t",
        execution_time=0.01,
    )
    _patch_client(monkeypatch, fake_result)

    rc = main(["eval", "1+1", "--quiet"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_eval_passes_timeout(monkeypatch):
    fake_result = VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")
    fake = _patch_client(monkeypatch, fake_result)

    rc = main(["eval", "1+1", "--timeout", "120", "--quiet"])
    assert rc == 0
    assert fake.last_timeout == 120
    assert fake.connection_timeout == 120


def test_eval_wraps_in_progn_for_multi_statement(monkeypatch):
    """Multi-statement input should Just Work, also against daemons from
    older releases whose single-line path takes only one form.  We do
    this by always sending ``progn(\\n<user>\\n)``."""
    fake_result = VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"ret"')
    fake = _patch_client(monkeypatch, fake_result)

    rc = main(["eval", 'printf("x") "ret"', "--quiet"])
    assert rc == 0
    assert fake.last_skill is not None
    assert fake.last_skill.startswith("progn(\n")
    assert fake.last_skill.endswith("\n)")
    assert 'printf("x") "ret"' in fake.last_skill


def test_eval_preserves_user_newlines(monkeypatch):
    fake_result = VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t")
    fake = _patch_client(monkeypatch, fake_result)
    multiline = 'let(((x 5))\n  x*x\n)'
    monkeypatch.setattr("sys.stdin", io.StringIO(multiline))

    rc = main(["eval", "--stdin", "--quiet"])
    assert rc == 0
    # The user's exact newlines must round-trip into the wrapped form
    # (otherwise SKILL line numbers in errors would shift).
    assert multiline in fake.last_skill


def test_eval_trailing_line_comment_does_not_swallow_closing_paren(monkeypatch):
    """Wrapping must add a newline before ``)``, otherwise a trailing
    ``; comment`` consumes the closing paren and the form errors out."""
    fake_result = VirtuosoResult(status=ExecutionStatus.SUCCESS, output="2")
    fake = _patch_client(monkeypatch, fake_result)

    rc = main(["eval", "1+1 ; trailing comment", "--quiet"])
    assert rc == 0
    # The comment must be terminated by a newline before the closing `)`.
    assert fake.last_skill.endswith("\n)")


def test_eval_stdout_is_only_json_while_env_and_ssh_echoes_go_to_stderr(
    monkeypatch, capsys, tmp_path
):
    """``virtuoso-bridge eval ... | jq`` must parse: the .env banner and the
    tunnel's ``[cmd]`` echoes are diagnostics and belong on stderr."""
    from virtuoso_bridge import cli
    from virtuoso_bridge.transport.ssh import SSHRunner

    env_file = tmp_path / ".env"
    fake = _FakeClient(VirtuosoResult(status=ExecutionStatus.SUCCESS, output="42"))

    class _EchoingVirtuosoClient:
        @classmethod
        def from_env(cls, profile=None, timeout=30):
            SSHRunner("eda-host", verbose=True)._print_cmd(["ssh", "eda-host", "cat", "token"])
            return fake

    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _EchoingVirtuosoClient)
    monkeypatch.setattr(cli, "load_vb_env", lambda: env_file)
    monkeypatch.setattr(cli, "_PRINTED_ENV_PATH", None)

    rc = main(["eval", "x = 1 x + 41"])

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["output"] == "42"
    assert f"using .env: {env_file}" in captured.err
    assert "[cmd] ssh eda-host cat token" in captured.err

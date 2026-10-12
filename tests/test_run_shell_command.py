"""run_shell_command returns the command's output, not csh()'s t/nil."""

from __future__ import annotations

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


def _client(monkeypatch, output):
    client = VirtuosoClient(host="127.0.0.1", port=9, timeout=5)
    sent = []

    def execute(skill, timeout=None, **_kwargs):
        sent.append(skill)
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=output)

    monkeypatch.setattr(client, "execute_skill", execute)
    return client, sent


def test_output_and_exit_status_are_returned(monkeypatch):
    client, sent = _client(monkeypatch, '(t "Linux 3.10\\ndesigner\\n")')

    result = client.run_shell_command('uname -sr; echo "$USER"')

    assert result.ok and result.output == "Linux 3.10\ndesigner\n"
    skill = sent[0]
    # The command is written verbatim to a script that a subshell sources,
    # so csh sees its quotes unchanged; output is private and deleted.
    assert 'fprintf(vbPort "%s\\n" "uname -sr; echo \\"$USER\\"")' in skill
    assert 'csh(strcat("umask 077; ( source " vbScript " ) >& " vbOut))' in skill
    assert skill.index("fprintf(") < skill.index("csh(") < skill.index("infile(vbOut)")
    assert skill.index("infile(vbOut)") < skill.index("deleteFile(vbScript)")
    assert skill.index("infile(vbOut)") < skill.index("deleteFile(vbOut)")


def test_a_failing_command_is_an_error_with_its_output(monkeypatch):
    client, _ = _client(monkeypatch, '(nil "ls: cannot access /nope: No such file or directory\\n")')

    result = client.run_shell_command("ls /nope")

    assert result.status == ExecutionStatus.ERROR
    assert result.output.startswith("ls: cannot access")
    assert result.errors == ["Shell command failed: ls /nope (ls: cannot access /nope: No such file or directory)"]


def test_malformed_reply_is_reported(monkeypatch):
    client, _ = _client(monkeypatch, "t")

    result = client.run_shell_command("true")

    assert result.status == ExecutionStatus.ERROR and "malformed" in result.errors[0]


def test_a_multi_line_command_keeps_its_line_breaks(monkeypatch):
    client, sent = _client(monkeypatch, '(t "line1\\nline2\\n")')

    client.run_shell_command("echo line1\necho line2")

    assert 'fprintf(vbPort "%s\\n" "echo line1\\necho line2")' in sent[0]
    assert "\n" not in sent[0]

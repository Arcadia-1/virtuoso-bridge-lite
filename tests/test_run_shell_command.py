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
    # A private 0700 temp directory is created before writing the command script,
    # so neither script nor output is world-readable on shared hosts.
    assert 'csh(strcat("umask 077; mkdir -m 700 " vbDir))' in skill
    assert 'vbScript = strcat(vbDir "/cmd")' in skill
    assert 'vbOut = strcat(vbDir "/out")' in skill
    assert 'fprintf(vbPort "%s\\n" "uname -sr; echo \\"$USER\\"")' in skill
    assert 'csh(strcat("umask 077; ( source " vbScript " ) >& " vbOut))' in skill
    assert skill.index("mkdir -m 700") < skill.index("outfile(vbScript)")
    assert skill.index("fprintf(") < skill.index("source ") < skill.index("infile(vbOut)")
    assert skill.index("infile(vbOut)") < skill.index("deleteFile(vbScript)")
    assert skill.index("infile(vbOut)") < skill.index("deleteFile(vbOut)")
    assert skill.index("deleteFile(vbOut)") < skill.index("deleteDir(vbDir)")


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


def test_crlf_carriage_returns_are_stripped(monkeypatch):
    client, sent = _client(monkeypatch, '(t "a\\nb\\n")')

    result = client.run_shell_command("echo a\r\necho b\r\n")

    assert result.ok
    assert 'fprintf(vbPort "%s\\n" "echo a\\necho b\\n")' in sent[0]
    assert "\r" not in sent[0]
    assert "\n" not in sent[0]


def test_private_temp_dir_creation_uses_0700_under_default_umask(tmp_path):
    import os
    import shutil
    import subprocess

    shell = shutil.which("csh") or shutil.which("sh")
    if os.name != "posix" or not shell:
        import pytest
        pytest.skip("requires POSIX shell")
    target = tmp_path / "vb_shell_probe"
    old_mask = os.umask(0o022)
    try:
        proc = subprocess.run(
            [shell, "-c", f"umask 077; mkdir -m 700 {target}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        os.umask(old_mask)
    assert proc.returncode == 0, proc.stderr
    assert (target.stat().st_mode & 0o777) == 0o700
    # Second mkdir on the same path fails atomically (no pre-created symlink hijack).
    again = subprocess.run(
        [shell, "-c", f"umask 077; mkdir -m 700 {target}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert again.returncode != 0

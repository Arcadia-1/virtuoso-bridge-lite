"""Regression tests for CLI -p/--profile forwarding and scripts/fix-symlinks.sh."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import virtuoso_bridge
from virtuoso_bridge import cli
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult

ROOT = Path(__file__).resolve().parents[1]
FIX_SYMLINKS = ROOT / "scripts" / "fix-symlinks.sh"

posix_bash_git = pytest.mark.skipif(
    os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
    reason="requires POSIX bash and git",
)


# -- CLI -p/--profile forwarding for windows, snapshot, screenshot -----------


class _FakeProfileClient:
    """Records profile passed to from_env() and verifies context-manager close."""

    calls: list[dict] = []
    closed: int = 0

    def __init__(self, profile: str | None) -> None:
        self.profile = profile

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.closed = 0

    @classmethod
    def from_env(cls, *, profile: str | None = None, **kwargs):
        cls.calls.append({"profile": profile, **kwargs})
        return cls(profile)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        type(self).closed += 1

    def list_windows(self):
        return [{"num": "1", "name": "CIW"}]

    def execute_skill(self, expr: str, timeout=None):
        if "hiGetWindowName" in expr:
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"Virtuoso Schematic Editor"')
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='("1" "" nil)')

    def screenshot(self, *, output=None, target="ciw"):
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=str(output or "/tmp/ciw.png"))


@pytest.fixture
def patched_cli_client(monkeypatch):
    _FakeProfileClient.reset()
    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FakeProfileClient)
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setitem(cli._SNAPSHOT_OPTS, "output_root", None)
    monkeypatch.setitem(cli._SNAPSHOT_OPTS, "json", False)
    monkeypatch.setitem(cli._SNAPSHOT_OPTS, "history", None)
    monkeypatch.setattr(cli, "_SCREENSHOT_TARGET", ["ciw"])
    monkeypatch.setattr(cli, "_SCREENSHOT_OUTPUT", [None])
    return _FakeProfileClient


@pytest.mark.parametrize(
    "argv",
    [
        ["windows", "-p", "lab2"],
        ["snapshot", "-p", "lab2"],
        ["screenshot", "ciw", "-p", "lab2"],
    ],
)
def test_windows_snapshot_screenshot_forward_profile_and_close_client(
    patched_cli_client, argv, capsys,
):
    rc = cli.main(argv)
    assert rc == 0, capsys.readouterr()
    assert patched_cli_client.calls == [{"profile": "lab2"}]
    assert patched_cli_client.closed == 1


@pytest.mark.parametrize("subcmd", ["windows", "snapshot", "screenshot"])
def test_registered_entrypoint_forwards_profile_to_client(tmp_path, subcmd):
    script = shutil.which("virtuoso-bridge", path=str(Path(sys.executable).parent))
    if script is None:
        pytest.skip("the virtuoso-bridge console script is not installed in this environment")
    env_file = tmp_path / "profiles.env"
    # Configure default host, intentionally leave profile 'lab2' unconfigured so
    # VirtuosoClient.from_env(profile="lab2") fails naming 'lab2'.
    env_file.write_text("VB_REMOTE_HOST=default-host.example\n", encoding="utf-8")
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("VB_") and k not in {"HOME", "USERPROFILE", "VIRTUAL_ENV"}
    }
    env.update(
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        VB_LOG_DIR=str(tmp_path / "logs"),
        VB_STATE_DIR=str(tmp_path / "state"),
    )

    result = subprocess.run(
        [script, subcmd, "-p", "lab2", "--env", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "profile 'lab2'" in result.stderr


# -- scripts/fix-symlinks.sh bounded real-bash tests -------------------------


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _setup_mock_repo(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create a real git repo with spaces in repo, symlink, and target paths."""
    repo = tmp_path / "Jane Doe" / "virtuoso bridge repo"
    (repo / "scripts").mkdir(parents=True)
    script_copy = repo / "scripts" / "fix-symlinks.sh"
    shutil.copy2(FIX_SYMLINKS, script_copy)

    target_dir = repo / "skills dir" / "virtuoso skill"
    target_dir.mkdir(parents=True)
    (target_dir / "SKILL.md").write_text("# Virtuoso\n", encoding="utf-8")

    rel_link = ".claude/my skills/virtuoso link"
    link_file = repo / rel_link
    link_file.parent.mkdir(parents=True)
    rel_target = "../../skills dir/virtuoso skill"
    link_file.write_text(rel_target, encoding="utf-8")

    # Initialize git repo and index the file as mode 120000 (symlink) while
    # keeping a plain text file in the working tree (core.symlinks=false).
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=20)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=rel_target,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"120000,{blob},{rel_link}"],
        cwd=repo,
        check=True,
        timeout=20,
    )
    return repo, script_copy, link_file, target_dir


def _make_mock_bin(tmp_path: Path, *, cmd_exit_code: int, uname_s: str = "MINGW64_NT-10.0") -> tuple[Path, Path]:
    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir()
    cmd_log = tmp_path / "cmd_args.log"

    _make_executable(
        mock_bin / "uname",
        f"#!/bin/sh\necho '{uname_s}'\n",
    )
    _make_executable(
        mock_bin / "cygpath",
        "#!/bin/sh\n"
        'if [ "$1" = "-w" ]; then shift; fi\n'
        'printf "C:%s\\n" "$1" | tr "/" "\\\\"\n',
    )
    _make_executable(
        mock_bin / "cmd",
        "#!/bin/sh\n"
        f'printf "MSYS_NO_PATHCONV=%s\\n" "${{MSYS_NO_PATHCONV:-}}" >> "{cmd_log}"\n'
        f'for arg in "$@"; do printf "ARG:%s\\n" "$arg" >> "{cmd_log}"; done\n'
        f"exit {cmd_exit_code}\n",
    )
    return mock_bin, cmd_log


@posix_bash_git
def test_fix_symlinks_handles_spaces_in_repo_and_paths(tmp_path):
    repo, script_copy, link_file, target_dir = _setup_mock_repo(tmp_path)
    mock_bin, cmd_log = _make_mock_bin(tmp_path, cmd_exit_code=0)

    env = dict(os.environ)
    env["PATH"] = f"{mock_bin}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(script_copy)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "Fixed 1 symlink(s)." in result.stdout
    lines = cmd_log.read_text(encoding="utf-8").splitlines()
    expected_link = "C:" + str(link_file.resolve()).replace("/", "\\")
    expected_target = "C:" + str(target_dir.resolve()).replace("/", "\\")
    assert lines == [
        "MSYS_NO_PATHCONV=1",
        "ARG:/c",
        "ARG:mklink",
        "ARG:/J",
        f"ARG:{expected_link}",
        f"ARG:{expected_target}",
    ]
    # Backup file was cleaned up after successful mklink.
    leftovers = list(link_file.parent.glob("*.vb-bak.*"))
    assert leftovers == []


@posix_bash_git
def test_fix_symlinks_restores_tracked_file_when_mklink_fails(tmp_path):
    repo, script_copy, link_file, _target_dir = _setup_mock_repo(tmp_path)
    original_text = link_file.read_text(encoding="utf-8")
    mock_bin, _cmd_log = _make_mock_bin(tmp_path, cmd_exit_code=1)

    env = dict(os.environ)
    env["PATH"] = f"{mock_bin}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(script_copy)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert "restored original file" in result.stderr
    assert link_file.is_file()
    assert link_file.read_text(encoding="utf-8") == original_text
    assert list(link_file.parent.glob("*.vb-bak.*")) == []


@posix_bash_git
def test_fix_symlinks_exits_cleanly_on_non_windows(tmp_path):
    repo, script_copy, link_file, _target_dir = _setup_mock_repo(tmp_path)
    original_text = link_file.read_text(encoding="utf-8")
    mock_bin, cmd_log = _make_mock_bin(tmp_path, cmd_exit_code=0, uname_s="Linux")

    env = dict(os.environ)
    env["PATH"] = f"{mock_bin}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(script_copy)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "Not on Windows" in result.stdout
    assert not cmd_log.exists()
    assert link_file.read_text(encoding="utf-8") == original_text

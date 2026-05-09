"""Tests for ``virtuoso-bridge load`` CLI command.

Covers the user-side input-validation paths that don't need a running
daemon: missing file and empty file return non-zero exit codes with
stderr messages, before any env / client instantiation happens.

Daemon-dependent paths (successful execute_skill, SKILL error
classification) require a real Virtuoso install and are out of scope
for this unit-test layer.
"""
from __future__ import annotations

from virtuoso_bridge.cli import main


def test_load_missing_file_returns_2(tmp_path, capsys):
    rc = main(["load", str(tmp_path / "nonexistent.il")])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err.lower()
    # Should not print env info before failing -- error path is
    # supposed to short-circuit before _load_cli_env().
    assert "using .env" not in captured.out


def test_load_empty_file_returns_2(tmp_path, capsys):
    f = tmp_path / "empty.il"
    f.write_text("")
    rc = main(["load", str(f)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "empty" in captured.err.lower()


def test_load_whitespace_only_file_returns_2(tmp_path, capsys):
    """A file with only whitespace/newlines should be treated as empty."""
    f = tmp_path / "blank.il"
    f.write_text("\n   \n\t\n")
    rc = main(["load", str(f)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "empty" in captured.err.lower()

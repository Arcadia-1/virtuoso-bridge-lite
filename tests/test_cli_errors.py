"""A failing CLI command prints one error line, not a Python traceback.

The traceback is kept in the command log for debugging.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import virtuoso_bridge
from virtuoso_bridge.cli import main


def _drop_command_log_handlers() -> None:
    pkg_logger = logging.getLogger("virtuoso_bridge")
    for handler in list(pkg_logger.handlers):
        if getattr(handler, "_vb_cmd_log", False):
            pkg_logger.removeHandler(handler)
            handler.close()


@pytest.fixture
def log_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("VB_LOG_DIR", str(tmp_path / "logs"))
    _drop_command_log_handlers()
    yield tmp_path / "logs"
    _drop_command_log_handlers()


def _client_raising(monkeypatch, exc: BaseException) -> None:
    class _FailingVirtuosoClient:
        @classmethod
        def from_env(cls, profile=None):
            raise exc

    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FailingVirtuosoClient)
    monkeypatch.setattr("virtuoso_bridge.cli._load_cli_env", lambda: None)


def test_failed_command_prints_one_line_and_logs_the_traceback(monkeypatch, capsys, log_dir):
    _client_raising(monkeypatch, RuntimeError("Virtuoso daemon identity check failed: busy"))

    rc = main(["eval", "1"])

    err = capsys.readouterr().err
    assert rc == 1
    assert err.startswith("ERROR: Virtuoso daemon identity check failed: busy (traceback in ")
    assert err.count("\n") == 1 and "Traceback" not in err
    log = (log_dir / "commands.log").read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in log
    assert "RuntimeError: Virtuoso daemon identity check failed: busy" in log


def test_interrupted_command_exits_130_without_traceback(monkeypatch, capsys, log_dir):
    _client_raising(monkeypatch, KeyboardInterrupt())

    assert main(["eval", "1"]) == 130
    assert capsys.readouterr().err == "ERROR: interrupted\n"


def test_registered_entrypoint_reports_errors_without_traceback(tmp_path):
    script = shutil.which("virtuoso-bridge", path=str(Path(sys.executable).parent))
    if script is None:
        pytest.skip("the virtuoso-bridge console script is not installed in this environment")
    env_file = tmp_path / "empty.env"
    env_file.write_text("VB_REMOTE_HOST=\n", encoding="utf-8")
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("VB_") and key not in {"HOME", "USERPROFILE"}
    }
    env.update(
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        VB_LOG_DIR=str(tmp_path / "logs"),
        VB_STATE_DIR=str(tmp_path / "state"),
    )

    result = subprocess.run(
        [script, "eval", "1", "--env", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    assert "ERROR: No remote host is configured" in result.stderr
    assert "Traceback (most recent call last)" in (tmp_path / "logs" / "commands.log").read_text(
        encoding="utf-8"
    )

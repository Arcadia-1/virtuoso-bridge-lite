from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from virtuoso_bridge import runtime_paths


def test_runtime_paths_env_overrides(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VB_HOME", str(tmp_path / "home"))
    assert runtime_paths.config_dir() == tmp_path / "home" / "config"
    assert runtime_paths.state_dir() == tmp_path / "home" / "state"
    assert runtime_paths.cache_dir("skill_finder") == tmp_path / "home" / "cache" / "skill_finder"
    assert runtime_paths.log_dir() == tmp_path / "home" / "logs"
    assert runtime_paths.tmp_dir("x") == tmp_path / "home" / "tmp" / "x"
    assert runtime_paths.artifact_dir("screenshots") == tmp_path / "home" / "artifacts" / "screenshots"

    monkeypatch.setenv("VB_LOG_DIR", str(tmp_path / "specific-logs"))
    assert runtime_paths.command_log_file() == tmp_path / "specific-logs" / "commands.log"


def test_import_virtuoso_bridge_does_not_create_command_log(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "VB_LOG_DIR": str(log_dir),
    }

    subprocess.run(
        [sys.executable, "-c", "import virtuoso_bridge"],
        check=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert not log_dir.exists()


def test_command_log_is_created_lazily(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VB_LOG_DIR", str(tmp_path / "logs"))
    import logging
    from virtuoso_bridge.transport import ssh

    pkg_logger = logging.getLogger("virtuoso_bridge")
    for handler in list(pkg_logger.handlers):
        if getattr(handler, "_vb_cmd_log", False):
            pkg_logger.removeHandler(handler)
            handler.close()

    assert not (tmp_path / "logs").exists()
    ssh.SSHRunner(host="example.invalid", user="designer")

    assert (tmp_path / "logs" / "commands.log").exists()


def test_tunnel_state_reads_legacy_cache_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("VB_STATE_DIR", str(tmp_path / "new-state"))
    legacy = tmp_path / ".cache" / "virtuoso_bridge" / "state.json"
    from virtuoso_bridge.transport.tunnel import SSHClient, _write_private_json
    _write_private_json(legacy, {"mode": "local", "port": 65432})

    assert SSHClient.read_state(target=None) == {"mode": "local", "port": 65432}

"""Explicit config selection works without changing the agent's home."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def isolated_agent(tmp_path):
    agent_home = tmp_path / "isolated home"
    agent_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("VB_REMOTE_HOST=wrong-host\n", encoding="utf-8")
    config = tmp_path / "existing bridge.env"
    config.write_text("VB_REMOTE_HOST=fixture-host\nVB_REMOTE_PORT=65432\n", encoding="utf-8")
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("VB_") and key != "PYTHONPATH"
    }
    # Only the child process gets the simulated agent home, on Unix and Windows.
    environment.update(HOME=str(agent_home), USERPROFILE=str(agent_home))
    return agent_home, workspace, config, environment


@pytest.mark.parametrize("missing", [False, True])
def test_registered_cli_selects_explicit_env_in_isolated_home(isolated_agent, missing):
    agent_home, workspace, config, environment = isolated_agent
    selected = config.with_name("missing.env") if missing else config
    executable = Path(sys.executable).with_name(
        "virtuoso-bridge.exe" if os.name == "nt" else "virtuoso-bridge"
    )
    result = subprocess.run(
        [str(executable), "profile", "list", "--json", "--env", str(selected)],
        cwd=workspace, env=environment, capture_output=True, text=True, timeout=10,
    )
    if missing:
        assert result.returncode != 0
        assert "not found" in json.loads(result.stdout)["error"]
        assert "wrong-host" not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        rows = json.loads(result.stdout)
        assert len(rows) == 1
        assert rows[0]["profile"] is None
        assert rows[0]["host"] == "fixture-host"
    assert not (agent_home / ".virtuoso-bridge" / ".env").exists()


def test_python_runtime_env_selection_in_isolated_home(isolated_agent):
    agent_home, workspace, config, environment = isolated_agent
    result = subprocess.run(
        [sys.executable, "-c", "\n".join([
            "import os, sys",
            "from virtuoso_bridge.env import set_runtime_env_file, load_vb_env",
            "set_runtime_env_file(sys.argv[1])",
            "load_vb_env()",
            "print(os.environ['VB_REMOTE_HOST'])",
        ]), str(config)],
        cwd=workspace, env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "fixture-host"
    assert not (agent_home / ".virtuoso-bridge" / ".env").exists()

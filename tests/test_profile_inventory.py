from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from virtuoso_bridge.profile_inventory import list_profiles


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    for key in list(os.environ):
        if key.startswith("VB_"):
            monkeypatch.delenv(key)


def test_profile_list_json_explains_profile_and_global_sources(tmp_path):
    config = tmp_path / "bridge.env"
    config.write_text(
        "VB_REMOTE_HOST_alpha=compute-a\nVB_REMOTE_USER_alpha=designer\n"
        "VB_REMOTE_PORT_alpha=65111\nVB_LOCAL_PORT_alpha=65112\n"
        "VB_CADENCE_CSHRC=/shared/tools.sh\nVB_CADENCE_ENV_SHELL=sh\n"
        "VB_PROCESS_alpha=opaque-process\nVB_PROCESS_VERSION_alpha=revision-2\n",
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items() if not key.startswith("VB_")}
    environment["PYTHONPATH"] = "src"
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from virtuoso_bridge.cli import main; sys.exit(main())",
         "profile", "list", "--json", "--env", str(config)],
        env=environment, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    alpha = next(row for row in rows if row["profile"] == "alpha")
    assert (alpha["host"], alpha["port"], alpha["local_port"]) == ("compute-a", 65111, 65112)
    assert alpha["process_version"] == "revision-2"
    assert alpha["process_env"] == "/shared/tools.sh"
    assert alpha["sources"]["host"]["key"] == "VB_REMOTE_HOST_alpha"
    assert alpha["sources"]["process_env"]["key"] == "VB_CADENCE_CSHRC"
    assert alpha["sources"]["process_env"]["inherited"] is True
    assert alpha["runtime_verified"] is False


def test_inventory_preserves_case_split_host_fallbacks_and_process_environment(tmp_path, monkeypatch):
    config = tmp_path / "bridge.env"
    config.write_text(
        "VB_REMOTE_HOST=default-host\nVB_REMOTE_PORT=65432\n"
        "VB_GUI_HOST_Alpha=gui-a\nVB_DAEMON_HOST_Alpha=compute-a\n"
        "VB_REMOTE_PORT_Alpha=65101\nVB_REMOTE_HOST_alpha=compute-b\n"
        "VB_REMOTE_PORT_alpha=65102\nVB_PROCESS_VARIANT_Alpha=validated\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VB_PROFILE", "Alpha")
    monkeypatch.setenv("VB_REMOTE_HOST_alpha", "overridden-by-file")
    before = dict(os.environ)

    rows = list_profiles(env_file=config)

    assert [row["profile"] for row in rows] == [None, "Alpha", "alpha"]
    assert rows[0]["host"] == "default-host"
    assert (rows[1]["gui_host"], rows[1]["deploy_host"], rows[1]["host"]) == ("gui-a", "gui-a", "compute-a")
    assert rows[1]["local_port"] == 65101
    assert rows[1]["sources"]["deploy_host"]["key"] == "VB_GUI_HOST_Alpha"
    assert rows[2]["host"] == "compute-b"
    assert os.environ == before


def test_invalid_inventory_is_machine_readable_and_fails_validation(tmp_path):
    config = tmp_path / "bridge.env"
    config.write_text(
        "VB_REMOTE_HOST_bad=compute-a\nVB_REMOTE_PORT_bad=70000\n"
        "VB_CADENCE_ENV_SHELL_bad=bash\nVB_PROCESS_orphan=opaque\n",
        encoding="utf-8",
    )
    environment = dict(os.environ, PYTHONPATH="src")
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from virtuoso_bridge.cli import main; sys.exit(main())",
         "profile", "list", "--json", "--env", str(config)],
        env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    rows = json.loads(result.stdout)
    assert len(rows) == 2
    assert all(not row["resolved"] for row in rows)
    assert len(rows[0]["errors"]) == 2
    assert rows[1]["errors"] == ["No daemon/legacy host configured"]


def test_default_ports_and_global_settings_are_not_phantom_profiles(tmp_path):
    config = tmp_path / "bridge.env"
    config.write_text(
        "VB_REMOTE_HOST_alpha=host-a\nVB_REMOTE_USER_alpha=designer\n"
        "VB_CADENCE_ENV_SHELL=sh\nVB_PROCESS_VERSION=r2\n"
        "VB_REMOTE_SCRATCH_ROOT=/shared\nVB_SPECTRE_BIN=/eda/spectre\n"
        "VB_DAEMON_TOKEN=must-not-leak\n",
        encoding="utf-8",
    )
    rows = list_profiles(env_file=config)
    assert [row["profile"] for row in rows] == ["alpha"]
    assert rows[0]["port"] == rows[0]["local_port"]
    assert rows[0]["sources"]["port"]["kind"] == "default"
    assert rows[0]["process_version"] == "r2"
    assert "must-not-leak" not in json.dumps(rows)

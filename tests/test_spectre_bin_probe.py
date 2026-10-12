"""VB_SPECTRE_BIN is probed exactly as a simulation runs it.

Sites that start Cadence tools through a wrapper configure e.g.
``VB_SPECTRE_BIN=site-wrapper spectre``. Simulations split that into the
wrapper plus its arguments; the status/license probe must do the same, and a
command that does not exist must not be reported as OK. The probe shell runs
for real with the local sh.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from virtuoso_bridge.spectre.runner import (
    parse_spectre_probe,
    probe_spectre_locally,
    spectre_probe_script,
)
from virtuoso_bridge.transport.ssh import CommandResult

pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="requires a POSIX shell")

VERSION = "@(#)$CDS: fixture wrapper version"


class ShellRunner:
    def run_command(self, command: str, timeout=None) -> CommandResult:
        proc = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=30)
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


@pytest.fixture
def wrapper(tmp_path):
    """A site wrapper that only runs tools named as its first argument and,
    like real ones, prints an environment banner before the tool's output."""
    path = tmp_path / "site wrapper"
    path.write_text(
        '#!/bin/sh\n[ "$1" = spectre ] || exit 9\nshift\necho "INFO site environment check"\n'
        f"[ \"$1\" = -V ] && echo '{VERSION}'\nexit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return f"'{path}' spectre"


def _probe(command: str) -> tuple[str | None, str | None]:
    result = ShellRunner().run_command(spectre_probe_script(command))
    return parse_spectre_probe(result.stdout)


def test_a_wrapper_with_arguments_is_probed_like_a_simulation(wrapper):
    assert _probe(wrapper) == (wrapper, VERSION)
    assert probe_spectre_locally(wrapper) == (wrapper, VERSION)


def test_a_missing_command_is_not_found(tmp_path):
    missing = str(tmp_path / "no-such-spectre")
    assert _probe(missing) == (None, None)
    assert _probe(f"{missing} spectre") == (None, None)
    assert probe_spectre_locally(missing) == (None, None)


def test_without_a_command_spectre_on_path_is_used(tmp_path, monkeypatch):
    tool = tmp_path / "bin" / "spectre"
    tool.parent.mkdir()
    tool.write_text(f"#!/bin/sh\necho '{VERSION}'\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tool.parent}{os.pathsep}{os.environ['PATH']}")
    assert _probe("") == (str(tool), VERSION)


@pytest.fixture
def bridge_env(tmp_path, monkeypatch):
    """Pin the bridge to a fixture .env so no user configuration leaks in."""
    from virtuoso_bridge.env import get_runtime_env_file, set_runtime_env_file

    previous = get_runtime_env_file()
    config = tmp_path / "bridge.env"

    def use(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        config.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
        set_runtime_env_file(config)

    yield use
    set_runtime_env_file(previous)


def test_license_check_reports_the_wrapper_and_rejects_a_missing_one(wrapper, tmp_path, bridge_env):
    from virtuoso_bridge.spectre import SpectreSimulator

    bridge_env(VB_CADENCE_CSHRC="", VB_SPECTRE_BIN=wrapper)
    info = SpectreSimulator(remote_host="fixture-host", ssh_runner=ShellRunner()).check_license()
    assert info["ok"] and info["spectre_path"] == wrapper and info["version"] == VERSION

    bridge_env(VB_CADENCE_CSHRC="", VB_SPECTRE_BIN=str(tmp_path / "no-such-spectre"))
    info = SpectreSimulator(remote_host="fixture-host", ssh_runner=ShellRunner()).check_license()
    assert not info["ok"] and info["spectre_path"] is None


def _status(tmp_path, monkeypatch, capsys, spectre_bin: str) -> str:
    from virtuoso_bridge import cli
    from virtuoso_bridge.env import get_runtime_env_file, set_runtime_env_file

    monkeypatch.setenv("VB_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("VB_CADENCE_CSHRC", "")
    monkeypatch.setenv("VB_SPECTRE_BIN", spectre_bin)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    config = tmp_path / "status.env"
    config.write_text(f"VB_REMOTE_HOST=localhost\nVB_CADENCE_CSHRC=\nVB_SPECTRE_BIN={spectre_bin}\n", encoding="utf-8")
    previous = get_runtime_env_file()
    try:
        cli.main(["status", "--env", str(config)])
    finally:
        set_runtime_env_file(previous)
    out = capsys.readouterr().out
    return out[out.index("[spectre]"):]


def test_status_reports_a_wrapper_with_its_version(wrapper, tmp_path, monkeypatch, capsys):
    out = _status(tmp_path, monkeypatch, capsys, wrapper)
    assert "[spectre] OK" in out and VERSION in out


def test_status_does_not_call_a_missing_command_ok(tmp_path, monkeypatch, capsys):
    missing = str(tmp_path / "no-such-spectre")
    out = _status(tmp_path, monkeypatch, capsys, missing)
    assert "[spectre] OK" not in out
    assert f"NOT FOUND: VB_SPECTRE_BIN={missing}" in out


def test_status_says_how_to_configure_spectre_when_none_is_found(tmp_path, monkeypatch, capsys):
    out = _status(tmp_path, monkeypatch, capsys, "")
    assert "[spectre] NOT FOUND" in out and "VB_SPECTRE_BIN" in out and "VB_CADENCE_CSHRC" in out

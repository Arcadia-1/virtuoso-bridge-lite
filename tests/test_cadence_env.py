from __future__ import annotations

import subprocess
import shutil
import shlex
import os
from types import SimpleNamespace

import pytest

from virtuoso_bridge.cadence_env import CadenceEnvironment

pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="requires POSIX shells")


def test_sh_selector_preserves_literal_environment_and_hides_banner(tmp_path):
    selector = tmp_path / "tools' env.sh"
    selector.write_text(
        "echo selector-banner\n"
        "export CDSHOME='/eda/release with spaces'\n"
        "export LM_LICENSE_FILE='27000@license; $(echo literal)'\n",
        encoding="utf-8",
    )
    command = CadenceEnvironment(str(selector), "sh").wrap(
        'printf "%s\\n%s\\n" "$CDSHOME" "$LM_LICENSE_FILE"'
    )

    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=5)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "/eda/release with spaces\n27000@license; $(echo literal)\n"


@pytest.mark.skipif(not shutil.which("csh"), reason="csh not installed")
def test_default_csh_selector_preserves_quoted_paths_and_values(tmp_path):
    selector = tmp_path / "tools' ! env.csh"
    selector.write_text(
        'echo selector-banner\nsetenv CDSHOME "/eda/release with spaces"\n',
        encoding="utf-8",
    )
    command = CadenceEnvironment(str(selector)).wrap(
        'printf "%s\\n" "$CDSHOME"'
    )

    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=5)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "/eda/release with spaces\n"


def test_profile_selects_its_script_and_shell_with_global_fallback(monkeypatch):
    monkeypatch.setenv("VB_CADENCE_CSHRC", "/global.csh")
    monkeypatch.setenv("VB_CADENCE_ENV_SHELL", "csh")
    monkeypatch.setenv("VB_CADENCE_CSHRC_worker", "/worker.sh")
    monkeypatch.setenv("VB_CADENCE_ENV_SHELL_worker", "sh")

    assert CadenceEnvironment.from_env("worker") == CadenceEnvironment("/worker.sh", "sh")
    assert CadenceEnvironment.from_env("other") == CadenceEnvironment("/global.csh", "csh")


class ShellRunner:
    """Exercise generated remote commands using a real, disposable local shell."""

    def run_command(self, command, timeout=5):
        import os
        import signal
        with subprocess.Popen(
            ["sh", "-c", command], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=min(timeout, 5))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            return SimpleNamespace(returncode=process.returncode, stdout=stdout, stderr=stderr)

    host = "fixture-host"

    def upload_batch(self, uploads):
        from pathlib import Path
        for source, destination in uploads:
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def download(self, source, destination, recursive=False):
        from pathlib import Path
        if not Path(source).exists():
            return SimpleNamespace(returncode=1, stdout="", stderr="missing optional artifact")
        if recursive:
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def sh_tools(monkeypatch, tmp_path):
    install = tmp_path / "eda tools"
    bindir = install / "bin"
    bindir.mkdir(parents=True)
    finder = install / "doc/finder/SKILL"
    finder.mkdir(parents=True)
    for tool in ("virtuoso", "spectre"):
        executable = bindir / tool
        executable.write_text("#!/bin/sh\nprintf '%s\\n' '@(#)$CDS: fixture version'\n", encoding="utf-8")
        executable.chmod(0o700)
    selector = tmp_path / "selector.sh"
    selector.write_text(
        f'export PATH="{bindir}:$PATH"\nexport CDSHOME="{install}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("VB_CADENCE_CSHRC_worker", str(selector))
    monkeypatch.setenv("VB_CADENCE_ENV_SHELL_worker", "sh")
    from virtuoso_bridge.env import get_runtime_env_file, set_runtime_env_file
    config = tmp_path / "selector.env"
    config.write_text('VB_CADENCE_ENV_SHELL_worker=sh\n', encoding="utf-8")
    previous = get_runtime_env_file()
    set_runtime_env_file(config)
    try:
        yield SimpleNamespace(install=install, finder=finder, selector=selector)
    finally:
        set_runtime_env_file(previous)


def test_skill_finder_uses_selected_sh_environment(sh_tools):
    from virtuoso_bridge.virtuoso.skill_finder import SKILLFinder

    assert SKILLFinder().discover(remote_runner=ShellRunner(), profile="worker") == sh_tools.finder


def test_docs_search_receives_selected_install_environment(sh_tools, monkeypatch):
    from virtuoso_bridge.virtuoso.docs_search import discover_remote_doc_roots

    # Remove the Finder anchor so CDSHOME is the only evidence for the doc root.
    monkeypatch.setenv("VB_CADENCE_CSHRC_worker", str(sh_tools.selector))
    (sh_tools.install / "bin/virtuoso").unlink()
    assert discover_remote_doc_roots(ShellRunner(), profile="worker") == [str(sh_tools.install / "doc")]


def test_spectre_probe_receives_sh_environment(sh_tools):
    from virtuoso_bridge.spectre import SpectreSimulator

    simulator = SpectreSimulator(
        remote_host="fixture-host", ssh_runner=ShellRunner(), profile="worker"
    )
    info = simulator.check_license()
    assert info["ok"], info
    assert info["spectre_path"] == str(sh_tools.install / "bin/spectre")


def test_cli_status_uses_sh_selector_in_local_mode(sh_tools, tmp_path, monkeypatch, capsys):
    from virtuoso_bridge import cli
    from virtuoso_bridge.env import get_runtime_env_file, set_runtime_env_file

    monkeypatch.setenv("VB_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("VB_REMOTE_HOST_worker", "localhost")
    monkeypatch.setenv("VB_SPECTRE_BIN", "")
    monkeypatch.setenv("VB_SPECTRE_BIN_worker", "")
    config = tmp_path / "bridge.env"
    config.write_text("VB_REMOTE_HOST_worker=localhost\n", encoding="utf-8")
    previous = get_runtime_env_file()
    try:
        cli.main(["status", "-p", "worker", "--env", str(config)])
        assert "[spectre] OK" in capsys.readouterr().out
    finally:
        set_runtime_env_file(previous)


def test_local_simulation_runs_in_selected_sh_environment(sh_tools, tmp_path):
    from virtuoso_bridge.spectre import SpectreSimulator

    executable = sh_tools.install / "bin/spectre"
    executable.write_text('#!/bin/sh\n[ -n "$CDSHOME" ] || exit 7\n', encoding="utf-8")
    netlist = tmp_path / "fixture.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    simulator = SpectreSimulator(
        spectre_cmd=shlex.quote(str(executable)), work_dir=tmp_path / "run", profile="worker"
    )
    result = simulator.run_simulation(netlist)
    assert result.ok, result.errors


def test_remote_simulation_uses_profile_selector_not_global(sh_tools, tmp_path, monkeypatch):
    from virtuoso_bridge.spectre import SpectreSimulator

    monkeypatch.setenv("VB_CADENCE_CSHRC", str(tmp_path / "wrong-selector.csh"))
    monkeypatch.setenv("VB_MENTOR_CSHRC", "")
    executable = sh_tools.install / "bin/spectre"
    executable.write_text('#!/bin/sh\n[ -n "$CDSHOME" ] || exit 7\n', encoding="utf-8")
    netlist = tmp_path / "fixture.scs"
    netlist.write_text("simulator lang=spectre\n", encoding="utf-8")
    simulator = SpectreSimulator(
        spectre_cmd=shlex.quote(str(executable)), profile="worker",
        remote_host="fixture-host", ssh_runner=ShellRunner(),
        remote_work_dir=str(tmp_path / "remote"), work_dir=tmp_path / "download",
        keep_remote_files=True,
    )
    result = simulator.run_simulation(netlist)
    assert result.ok, result.errors


@pytest.mark.parametrize("shell", ["sh", "csh"])
def test_selector_failure_never_runs_tool(tmp_path, shell):
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    selector = tmp_path / "selector"
    selector.write_text("exit 7\n", encoding="utf-8")
    result = ShellRunner().run_command(CadenceEnvironment(str(selector), shell).wrap("echo tool-was-run"))
    assert result.returncode != 0
    assert result.stdout == ""


def test_invalid_shell_is_rejected_without_execution():
    with pytest.raises(ValueError, match="csh or sh"):
        CadenceEnvironment("selector", "bash")


def test_local_license_probe_uses_same_selector_as_simulation(sh_tools):
    from virtuoso_bridge.spectre import SpectreSimulator

    info = SpectreSimulator(profile="worker").check_license()
    assert info["ok"], info
    assert info["version"] == "@(#)$CDS: fixture version"


@pytest.mark.skipif(not shutil.which("csh"), reason="csh not installed")
def test_legacy_csh_selector_can_handle_unsuccessful_optional_probe(tmp_path):
    selector = tmp_path / "legacy.csh"
    selector.write_text('false\nsetenv CDSHOME /eda/legacy\n', encoding="utf-8")
    result = ShellRunner().run_command(
        CadenceEnvironment(str(selector)).wrap('printf "%s" "$CDSHOME"')
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "/eda/legacy"

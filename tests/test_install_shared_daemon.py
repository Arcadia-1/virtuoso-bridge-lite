"""Real Linux publication/mode checks; ownership rejection is separately mocked."""
import os
from pathlib import Path
import sys
import tempfile
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from virtuoso_bridge import daemon_setup as installer


def _cli(*args):
    script = shutil.which("virtuoso-bridge", path=str(Path(sys.executable).parent))
    assert script, "Install the project in its virtualenv before running CLI tests"
    return subprocess.run([script, *args], capture_output=True, text=True, timeout=30)


def test_installed_command_help():
    result = _cli("install-daemon", "--help")
    assert result.returncode == 0, result.stderr
    assert "--python" in result.stdout
    assert "--scratch-root" in result.stdout
    assert "Linux Cadence host" in result.stdout


def test_cli_dispatch_without_connection_configuration(monkeypatch, tmp_path, capsys):
    from virtuoso_bridge.cli import main

    destination = tmp_path / "bundle"
    calls = []
    def install(path, python, scratch_root):
        calls.append((path, python, scratch_root))
        return path / "virtuoso_setup.il"
    monkeypatch.setattr(installer, "install", install)
    def unexpected(*args, **kwargs):
        pytest.fail("installation must not resolve a client connection")
    monkeypatch.setattr("virtuoso_bridge.cli.set_runtime_env_file", unexpected)
    monkeypatch.setattr("virtuoso_bridge.profile.resolve_profile", unexpected)
    assert main(["install-daemon", str(destination), "--python", "/usr/bin/python3",
                 "--scratch-root", "~/bridge"]) == 0
    assert calls == [(destination, "/usr/bin/python3", "~/bridge")]
    assert 'load("' + (destination / "virtuoso_setup.il").as_posix() + '")' in capsys.readouterr().out


def test_cli_publish_and_refuse_overwrite(private_parent, monkeypatch):
    monkeypatch.setenv("VB_LOG_DIR", str(private_parent / "logs"))
    destination = private_parent / "cli-bundle"
    result = _cli("install-daemon", str(destination), "--python", sys.executable,
                  "--scratch-root", str(private_parent))
    assert result.returncode == 0, result.stderr
    assert 'load("%s/virtuoso_setup.il")' % destination.as_posix() in result.stdout
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    result = _cli("install-daemon", str(destination))
    assert result.returncode == 1
    assert "ERROR:" in result.stderr and "Traceback" not in result.stderr
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == before


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="unsupported host check")
def test_cli_rejects_unsupported_host(tmp_path, monkeypatch):
    monkeypatch.setenv("VB_LOG_DIR", str(tmp_path / "logs"))
    destination = tmp_path / "bundle"
    result = _cli("install-daemon", str(destination))
    assert result.returncode == 1
    assert "Linux Cadence host" in result.stderr
    assert not destination.exists()


def test_registered_setup_preserves_preload_port_and_registration():
    from virtuoso_bridge.transport.tunnel import _generate_virtuoso_setup_il

    setup = _generate_virtuoso_setup_il("/bundle/daemon.py", "/bundle/ramic_bridge.il", port=0)
    assert 'unless(getShellEnvVar("RB_PORT")\nsetShellEnvVar("RB_PORT" "0")\n)' in setup
    assert 'setShellEnvVar("RB_AUTO_PORT" "1")' in setup
    legacy = _generate_virtuoso_setup_il("/bundle/daemon.py", "/bundle/ramic_bridge.il", port=65123)
    assert 'unless(getShellEnvVar("RB_PORT")' not in legacy
    assert 'setShellEnvVar("RB_PORT" "65123")' in legacy
    assert 'setShellEnvVar("RB_AUTO_PORT" "0")' in legacy


def test_unsupported_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.sys, "platform", "win32")
    with pytest.raises(ValueError, match="Linux"):
        installer.install(tmp_path / "bundle")


@pytest.fixture
def private_parent():
    if not sys.platform.startswith("linux"):
        pytest.skip("real Linux permission and exclusive-publication checks")
    # /tmp is intentionally unsuitable as a shared-code ancestor.
    with tempfile.TemporaryDirectory(prefix=".vb-installer-test-", dir=Path.home()) as path:
        yield Path(path)


@pytest.mark.parametrize("python,worker", [("/usr/bin/python3", "3"), ("/opt/python/bin/python2.7", "27")])
def test_publish_under_umask_zero_without_overwrite(private_parent, python, worker):
    destination = private_parent / "v1"
    mask = os.umask(0)
    try:
        setup = installer.install(destination, python, "~/private root")
        assert os.umask(0) == 0
    finally:
        os.umask(mask)
    assert setup == destination.resolve() / "virtuoso_setup.il"
    assert destination.stat().st_mode & 0o777 == 0o755
    assert all(p.stat().st_mode & 0o777 == 0o644 for p in destination.iterdir())
    source = setup.read_text()
    assert "ramic_bridge_daemon_%s.py" % worker in source
    assert python in source
    assert 'setShellEnvVar("RB_PORT" "0")' in source
    assert "~/private root" in source
    with pytest.raises(FileExistsError):
        installer.install(destination)
    assert setup.read_text() == source


@pytest.mark.parametrize("kind", ["writable", "symlink", "foreign-owner"])
def test_reject_unsafe_ancestor(private_parent, monkeypatch, kind):
    parent = private_parent / "unsafe"
    parent.mkdir(mode=0o700)
    if kind == "writable":
        parent.chmod(0o777)
    elif kind == "symlink":
        link = private_parent / "link"
        link.symlink_to(parent)
        parent = link
    else:
        original = Path.lstat
        def fake_stat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == parent:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 10000)
            return info
        monkeypatch.setattr(Path, "lstat", fake_stat)
    with pytest.raises(ValueError, match="real parents"):
        installer.install(parent / "v1")
    assert not (parent / "v1").exists()

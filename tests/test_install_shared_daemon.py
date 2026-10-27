"""Real Linux publication/mode checks; ownership rejection is separately mocked."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("shared_installer", Path(__file__).parents[1] / "tools/install_shared_daemon.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


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

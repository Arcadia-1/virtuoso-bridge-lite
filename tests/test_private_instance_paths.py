"""Real Linux directory and shell checks; no SSH or Cadence required."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptors, ownership and POSIX shell")


@pytest.mark.parametrize("operation", ["registry", "publish"])
@pytest.mark.parametrize("kind", ["writable_root", "readable_root", "writable_ancestor", "symlink_root", "symlink_ancestor"])
def test_unsafe_paths_refused_without_modification(tmp_path, operation, kind):
    from virtuoso_bridge.virtuoso.basic.resources import ramic_deploy, ramic_registry
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    root = parent / "private"
    root.mkdir(mode=0o700)
    if kind.endswith("ancestor"):
        if kind.startswith("symlink"):
            link = tmp_path / "link"
            link.symlink_to(parent, target_is_directory=True)
            root = link / "private"
        else:
            parent.chmod(0o777)
    elif kind == "symlink_root":
        link = parent / "link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    else:
        root.chmod(0o777 if kind == "writable_root" else 0o755)
    before = root.stat().st_mode
    with pytest.raises((ValueError, OSError)):
        if operation == "registry":
            ramic_registry.Registry(root=str(root))
        else:
            ramic_deploy.publish(str(root / "daemon"), {"code.py": "trusted"})
    assert root.stat().st_mode == before
    assert list(root.iterdir()) == []


def test_publication_uses_verified_descriptor_after_path_replacement(tmp_path, monkeypatch):
    from virtuoso_bridge.virtuoso.basic.resources import ramic_deploy
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    retained = tmp_path / "retained"
    original = ramic_deploy.private_directory
    def swapped(path, create):
        fd = original(path, create)
        root.rename(retained)
        root.mkdir(mode=0o700)
        return fd
    monkeypatch.setattr(ramic_deploy, "private_directory", swapped)
    ramic_deploy.publish(str(root / "daemon"), {"code.py": "trusted"})
    assert list(root.iterdir()) == []
    assert (retained / "daemon" / "code.py").read_text() == "trusted"


def test_registry_anchors_cleanup_to_verified_directory(tmp_path):
    from virtuoso_bridge.virtuoso.basic.resources import ramic_registry
    root = tmp_path / "private"
    registry = ramic_registry.Registry(root=str(root))
    old = tmp_path / "retained"
    try:
        root.rename(old)
        (root / "runs").mkdir(parents=True)
        name = "a" * 32 + ".json"
        (root / "runs" / name).write_text("untouched")
        stale = old / "runs" / name
        stale.write_text("old")
        stale.chmod(0o600)
        registry.sweep()
        assert not stale.exists()
        assert (root / "runs" / name).read_text() == "untouched"
    finally:
        registry.close()


def test_remote_publisher_executes_real_bounded_shell(tmp_path):
    from virtuoso_bridge.transport.tunnel import _ramic_script
    root = tmp_path / "new private root"
    destination = str(root / "daemon")
    payload = json.dumps(dict(destination=destination, files={"code.py": "trusted"}))
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(_ramic_script("ramic_deploy.py"))
    result = subprocess.run(["sh", "-s"], input=command + " <<'BUNDLE'\n" + payload + "\nBUNDLE\n",
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["path"] == destination
    assert (root / "daemon" / "code.py").read_text() == "trusted"
    # Discovery runs the same bundled helper without trusting remote cwd.
    result = subprocess.run([sys.executable, "-c", _ramic_script("ramic_registry.py"),
                             "--scratch-root", str(root), "--read-only"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

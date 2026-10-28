from __future__ import annotations

import shutil

import pytest


@pytest.fixture(autouse=True)
def _isolated_connection_state(monkeypatch, tmp_path, request):
    """Offline transport tests must never inspect a developer's saved tunnels."""
    if request.module.__name__.endswith("test_runtime_paths"):
        return
    monkeypatch.setenv("VB_STATE_DIR", str(tmp_path / "bridge-state"))


@pytest.fixture(autouse=True)
def _private_remote_daemon_cache(monkeypatch, tmp_path_factory):
    """Keep the remote-daemon token cache out of the real home directory."""
    root = tmp_path_factory.mktemp("remote_daemons")
    monkeypatch.setattr("virtuoso_bridge.daemon_auth._remote_cache_root", lambda: root)


@pytest.fixture(autouse=True)
def _portable_local_tar_commands(request, monkeypatch):
    """Build the portable local tar argv on every client platform.

    On macOS the bridge probes ``tar --version`` and adds bsdtar AppleDouble
    options. That probe would also reach tests' fake ``Popen`` and change the
    argv they assert. Tests of the probe and live tests request
    ``real_tar_detection``.
    """
    if "real_tar_detection" in request.fixturenames:
        return
    monkeypatch.setattr(
        "virtuoso_bridge.transport.transfer._is_macos_bsdtar",
        lambda _tar_command: False,
    )


@pytest.fixture
def real_tar_detection():
    """Use the bridge's real local tar probe, starting from an empty cache."""
    from virtuoso_bridge.transport import transfer

    transfer._tar_reports_bsdtar.cache_clear()
    yield
    transfer._tar_reports_bsdtar.cache_clear()


@pytest.fixture
def macos_bsdtar(real_tar_detection) -> str:
    """Return the local tar when it is macOS bsdtar; skip on other clients."""
    from virtuoso_bridge.transport import transfer

    tar_command = shutil.which("tar")
    if tar_command is None or not transfer._is_macos_bsdtar(tar_command):
        pytest.skip("requires macOS with bsdtar as the local tar")
    return tar_command

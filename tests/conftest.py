from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _private_remote_daemon_cache(monkeypatch, tmp_path_factory):
    """Keep the remote-daemon token cache out of the real home directory."""
    root = tmp_path_factory.mktemp("remote_daemons")
    monkeypatch.setattr("virtuoso_bridge.daemon_auth._remote_cache_root", lambda: root)

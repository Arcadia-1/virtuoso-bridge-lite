from __future__ import annotations

from virtuoso_bridge.transport.remote_paths import (
    default_virtuoso_bridge_dir,
    default_remote_spectre_work_dir,
    resolve_client_id,
)


def test_default_bridge_dir_uses_private_home_without_username_nesting(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)

    assert (
        default_virtuoso_bridge_dir("designer", "virtuoso_bridge_t28_io")
        == "~/.virtuoso-bridge/virtuoso_bridge_t28_io"
    )


def test_default_bridge_dir_can_be_client_scoped(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)

    assert (
        default_virtuoso_bridge_dir("designer", "virtuoso_bridge_t28_io", "90590")
        == "~/.virtuoso-bridge/90590/virtuoso_bridge_t28_io"
    )


def test_default_spectre_dir_can_be_client_scoped(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)

    assert (
        default_remote_spectre_work_dir("designer", "lab/pc:1")
        == "~/.virtuoso-bridge/lab_pc_1/virtuoso_bridge_spectre"
    )


def test_resolve_client_id_prefers_profile_env(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    monkeypatch.setenv("VB_CLIENT_ID_t28_io", "workstation_a")

    assert resolve_client_id("t28_io") == "workstation_a"
    assert resolve_client_id() == "90590"


def test_home_scratch_root_scopes_bridge_and_spectre_without_new_host_settings(monkeypatch):
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/home/designer/.virtuoso-bridge/")

    root = "/home/designer/.virtuoso-bridge/laptop"
    assert default_virtuoso_bridge_dir("designer", "virtuoso_bridge", "laptop") == (
        root + "/virtuoso_bridge"
    )
    assert default_remote_spectre_work_dir("designer", "laptop") == (
        root + "/virtuoso_bridge_spectre"
    )


def test_home_is_resolved_on_remote_host_once(monkeypatch):
    from types import SimpleNamespace
    from virtuoso_bridge.transport.remote_paths import remote_scratch_root
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    calls = []
    def run(command, timeout):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="/home/remote-user", stderr="")
    runner = SimpleNamespace(run_command=run)
    assert remote_scratch_root(runner) == "/home/remote-user/.virtuoso-bridge"
    assert default_virtuoso_bridge_dir("ignored", "daemon", runner=runner) == (
        "/home/remote-user/.virtuoso-bridge/daemon")
    assert calls == ['getent passwd "$(id -u)" | cut -d: -f6']


def test_invalid_home_or_relative_root_is_rejected(monkeypatch):
    import pytest
    from types import SimpleNamespace
    from virtuoso_bridge.transport.remote_paths import remote_scratch_root
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    runner = SimpleNamespace(run_command=lambda *a, **kw: SimpleNamespace(returncode=0, stdout=""))
    with pytest.raises(ValueError, match="remote account home"):
        remote_scratch_root(runner)
    for root in ("relative", "~/../shared", "$HOME/scratch"):
        monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", root)
        with pytest.raises(ValueError):
            remote_scratch_root()


def test_passwd_home_cache_is_per_runner_and_ignores_client_home(monkeypatch):
    from types import SimpleNamespace
    from virtuoso_bridge.transport.remote_paths import remote_scratch_root
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("HOME", "/wrong/client/home")
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/scratch with spaces")
    calls = []
    def runner(home):
        def run(command, timeout):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout=home + "\n")
        return SimpleNamespace(run_command=run)
    first, second = runner("/home/one"), runner("/home/two")
    assert remote_scratch_root(first) == "/home/one/scratch with spaces"
    assert remote_scratch_root(second) == "/home/two/scratch with spaces"
    assert remote_scratch_root(first) == "/home/one/scratch with spaces"
    assert len(calls) == 2

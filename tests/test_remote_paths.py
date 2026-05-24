from __future__ import annotations

from virtuoso_bridge.transport.remote_paths import (
    default_virtuoso_bridge_dir,
    default_remote_spectre_work_dir,
    resolve_client_id,
)


def test_default_bridge_dir_preserves_legacy_path_without_client_id(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)

    assert (
        default_virtuoso_bridge_dir("designer", "virtuoso_bridge_t28_io")
        == "/tmp/virtuoso_bridge_designer/virtuoso_bridge_t28_io"
    )


def test_default_bridge_dir_can_be_client_scoped(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)

    assert (
        default_virtuoso_bridge_dir("designer", "virtuoso_bridge_t28_io", "90590")
        == "/tmp/virtuoso_bridge_designer/90590/virtuoso_bridge_t28_io"
    )


def test_default_spectre_dir_can_be_client_scoped(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)

    assert (
        default_remote_spectre_work_dir("designer", "lab/pc:1")
        == "/tmp/virtuoso_bridge_designer/lab_pc_1/virtuoso_bridge_spectre"
    )


def test_resolve_client_id_prefers_profile_env(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    monkeypatch.setenv("VB_CLIENT_ID_t28_io", "workstation_a")

    assert resolve_client_id("t28_io") == "workstation_a"
    assert resolve_client_id() == "90590"

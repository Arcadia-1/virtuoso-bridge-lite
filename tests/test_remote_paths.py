from __future__ import annotations

from types import SimpleNamespace

import pytest

from virtuoso_bridge.transport.remote_paths import (
    default_virtuoso_bridge_dir,
    default_remote_spectre_work_dir,
    instance_scratch_root,
    remote_scratch_root,
    resolve_client_id,
    resolve_remote_scratch_root,
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


def test_home_scratch_root_scopes_bridge_and_spectre_without_new_host_settings(monkeypatch):
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/home/designer/.virtuoso-bridge/")

    root = "/home/designer/.virtuoso-bridge/virtuoso_bridge_designer/laptop"
    assert default_virtuoso_bridge_dir("designer", "virtuoso_bridge", "laptop") == (
        root + "/virtuoso_bridge"
    )
    assert default_remote_spectre_work_dir("designer", "laptop") == (
        root + "/virtuoso_bridge_spectre"
    )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, "/tmp"),
        ("", "/tmp"),
        ("   ", "/tmp"),
        ("/", "/"),
        ("///", "/"),
        ("/tmp/", "/tmp"),
        ("~/", "~"),
        ("~", "~"),
        ("~/scratch/", "~/scratch"),
    ],
)
def test_remote_scratch_root_normalizes_blank_root_and_tilde(
    monkeypatch, raw_value, expected
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    if raw_value is None:
        monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    else:
        monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", raw_value)

    assert remote_scratch_root() == expected


def test_blank_scratch_root_uses_tmp_and_explicit_slash_avoids_double_slash(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "")
    assert default_virtuoso_bridge_dir("designer", "virtuoso_bridge") == (
        "/tmp/virtuoso_bridge_designer/virtuoso_bridge"
    )

    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/")
    assert default_virtuoso_bridge_dir("designer", "virtuoso_bridge") == (
        "/virtuoso_bridge_designer/virtuoso_bridge"
    )


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("~/", "/home/remote"),
        ("~", "/home/remote"),
        ("~/scratch", "/home/remote/scratch"),
        ("~/scratch/", "/home/remote/scratch"),
    ],
)
def test_instance_and_remote_scratch_root_expand_tilde_via_remote_home(
    monkeypatch, configured, expected
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", configured)
    runner = SimpleNamespace(
        run_command=lambda command: SimpleNamespace(
            returncode=0 if "getent" in command else 1,
            stdout="/home/remote/\n" if "getent" in command else "",
            stderr="",
        )
    )

    assert resolve_remote_scratch_root(runner) == expected
    assert instance_scratch_root(runner) == expected


def test_tilde_expansion_falls_back_to_remote_home_env_when_getent_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/")
    commands: list[str] = []

    def fake_run(command: str):
        commands.append(command)
        if "getent" in command:
            return SimpleNamespace(returncode=127, stdout="", stderr="getent: not found")
        return SimpleNamespace(returncode=0, stdout="/Users/remote", stderr="")

    runner = SimpleNamespace(run_command=fake_run)
    assert resolve_remote_scratch_root(runner) == "/Users/remote"
    assert len(commands) == 2


@pytest.mark.parametrize(
    ("configured", "suffix"),
    [
        ("~", ""),
        ("~/", ""),
        ("~/vb_scratch", "/vb_scratch"),
        ("~/vb_scratch/", "/vb_scratch"),
    ],
)
def test_resolve_remote_scratch_root_expands_tilde_in_local_mode(
    monkeypatch, tmp_path, configured, suffix
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", configured)
    local_home = tmp_path / "local_home"
    monkeypatch.setenv("HOME", str(local_home))
    monkeypatch.setenv("USERPROFILE", str(local_home))

    resolved = resolve_remote_scratch_root(None)

    expected_home = str(local_home).rstrip("/")
    assert resolved == expected_home + suffix
    assert not resolved.startswith("~")


def test_default_bridge_and_spectre_dirs_expand_tilde_when_runner_given(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/vb_scratch")
    runner = SimpleNamespace(
        run_command=lambda command: SimpleNamespace(
            returncode=0 if "getent" in command else 1,
            stdout="/home/remote\n" if "getent" in command else "",
            stderr="",
        )
    )

    assert (
        default_virtuoso_bridge_dir("designer", "x11", "client1", runner=runner)
        == "/home/remote/vb_scratch/virtuoso_bridge_designer/client1/x11"
    )
    assert (
        default_remote_spectre_work_dir("designer", "client1", runner=runner)
        == "/home/remote/vb_scratch/virtuoso_bridge_designer/client1/virtuoso_bridge_spectre"
    )
    # Explicit root= still takes precedence over runner/env.
    assert (
        default_virtuoso_bridge_dir("designer", "x11", "client1", root="/explicit", runner=runner)
        == "/explicit/virtuoso_bridge_designer/client1/x11"
    )

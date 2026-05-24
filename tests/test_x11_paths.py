from __future__ import annotations

from pathlib import Path

from virtuoso_bridge.virtuoso import x11


class _FakeRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[Path, str]] = []

    def run_command(self, command: str, timeout=None):
        self.commands.append(command)

    def upload(self, local_path: Path, remote_path: str):
        self.uploads.append((local_path, remote_path))


def test_x11_helper_uses_client_scoped_bridge_dir(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    monkeypatch.delenv("VB_CLIENT_ID_t28_io", raising=False)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    runner = _FakeRunner()

    remote_path = x11._ensure_helper(runner, "designer")

    assert remote_path == "/tmp/virtuoso_bridge_designer/90590/x11/x11_dismiss_dialog.py"
    assert runner.commands == ["mkdir -p /tmp/virtuoso_bridge_designer/90590/x11"]
    assert runner.uploads[0][1] == remote_path


def test_x11_helper_uses_profile_scoped_client_id(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    monkeypatch.setenv("VB_CLIENT_ID_t28_io", "workstation_a")
    runner = _FakeRunner()

    remote_path = x11._ensure_helper(runner, "designer", profile="t28_io")

    assert remote_path == "/tmp/virtuoso_bridge_designer/workstation_a/x11/x11_dismiss_dialog.py"
    assert runner.commands == ["mkdir -p /tmp/virtuoso_bridge_designer/workstation_a/x11"]
    assert runner.uploads[0][1] == remote_path

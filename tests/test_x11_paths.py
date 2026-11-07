from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso import x11


class _FakeRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[Path, str]] = []

    def run_command(self, command: str, timeout=None):
        self.commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def upload(self, local_path: Path, remote_path: str, timeout=None):
        self.uploads.append((local_path, remote_path))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_x11_helper_uses_client_scoped_bridge_dir(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    monkeypatch.delenv("VB_CLIENT_ID_t28_io", raising=False)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    runner = _FakeRunner()

    remote_path = x11._ensure_helper(runner, "designer")

    assert remote_path == "/tmp/virtuoso_bridge_designer/90590/x11/x11_dismiss_dialog.py"
    assert runner.commands == ["mkdir -p /tmp/virtuoso_bridge_designer/90590/x11",
                               f"test -r {remote_path}"]
    assert runner.uploads[0][1] == remote_path


def test_x11_helper_uses_profile_scoped_client_id(monkeypatch) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    monkeypatch.setenv("VB_CLIENT_ID_t28_io", "workstation_a")
    runner = _FakeRunner()

    remote_path = x11._ensure_helper(runner, "designer", profile="t28_io")

    assert remote_path == "/tmp/virtuoso_bridge_designer/workstation_a/x11/x11_dismiss_dialog.py"
    assert runner.commands == ["mkdir -p /tmp/virtuoso_bridge_designer/workstation_a/x11",
                               f"test -r {remote_path}"]
    assert runner.uploads[0][1] == remote_path


@pytest.mark.parametrize("stage", ["directory", "upload", "readability"])
def test_deployment_failure_identifies_stage_and_never_returns_path(monkeypatch, stage):
    runner = _FakeRunner()
    original_run = runner.run_command
    def run(command, timeout=None):
        if (stage == "directory" and command.startswith("mkdir")) or (
                stage == "readability" and command.startswith("test -r")):
            return SimpleNamespace(returncode=1, stderr="permission denied")
        return original_run(command, timeout)
    monkeypatch.setattr(runner, "run_command", run)
    if stage == "upload":
        monkeypatch.setattr(runner, "upload", lambda *a, **k:
                            SimpleNamespace(returncode=1, stderr="upload failed"))
    with pytest.raises(x11.X11HelperDeploymentError) as caught:
        x11._deploy_helper(runner, x11._HELPER_SCRIPT, "/scratch/helper.py")
    assert caught.value.stage == stage
    assert caught.value.remote_target == "/scratch/helper.py"
    assert caught.value.local_helper == str(x11._HELPER_SCRIPT)
    assert caught.value.returncode == 1
    if stage == "directory":
        assert runner.uploads == []


def test_local_helper_needs_no_deployment():
    assert x11._ensure_helper(None, "user") == str(x11._HELPER_SCRIPT)


def test_deployment_commands_in_real_bounded_shell(tmp_path):
    import shutil
    import subprocess
    shell = shutil.which("sh")
    git = shutil.which("git")
    if git:
        git_shell = Path(git).parent.parent / "usr" / "bin" / "sh.exe"
        if git_shell.is_file():
            shell = str(git_shell)
    if not shell:
        pytest.skip("POSIX shell unavailable")
    class ShellRunner:
        def run_command(self, command, timeout):
            return subprocess.run([shell, "-c", command], capture_output=True,
                                  text=True, timeout=min(timeout, 5))
        def upload(self, local, remote, timeout):
            shutil.copyfile(local, remote)
            return SimpleNamespace(returncode=0, stderr="")
    target = tmp_path / "helper's space" / "helper.py"
    x11._deploy_helper(ShellRunner(), x11._HELPER_SCRIPT, target.as_posix(), timeout=5)
    assert target.read_bytes() == x11._HELPER_SCRIPT.read_bytes()


@pytest.mark.parametrize("operation", ["inspection", "close"])
def test_missing_cached_helpers_redeploy_before_execution(monkeypatch, operation):
    import json
    import shlex
    files = set()
    runner = _FakeRunner()
    original_upload = runner.upload
    def upload(local, remote, timeout=None):
        files.add(remote)
        return original_upload(local, remote, timeout)
    def command(text, timeout=None):
        runner.commands.append(text)
        code = int(shlex.split(text)[2] not in files) if text.startswith("test -r") else 0
        return SimpleNamespace(returncode=code, stdout="", stderr="")
    monkeypatch.setattr(runner, "upload", upload)
    monkeypatch.setattr(runner, "run_command", command)
    monkeypatch.setattr(x11, "_detect_remote_python", lambda *a, **k: "python3")
    monkeypatch.setattr(x11, "default_virtuoso_bridge_dir", lambda *a: "/scratch/space name")
    executions = []
    def execute(_runner, text, timeout):
        executions.append(text)
        assert files
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "clear"}), stderr="")
    monkeypatch.setattr(x11, "_run", execute)
    def call():
        if operation == "inspection":
            return x11.inspect_dialogs(runner, "user", pid=42)
        return x11.dialog_close_exchange(runner, "user", {"op": "prepare"})
    call()
    cold_uploads = len(runner.uploads)
    call()
    assert len(runner.uploads) == cold_uploads
    # The close helper imports the inspection helper; verify both files.
    missing = next(path for path in files if "x11_dismiss" in path) if operation == "close" else next(iter(files))
    files.remove(missing)
    call()
    assert len(runner.uploads) == 2 * cold_uploads
    assert len(executions) == 3


def test_cached_readability_transport_failure_never_redeploys(monkeypatch):
    runner = _FakeRunner()
    runner._vb_dialog_helper = (("user", None, "digest"), "/helper.py", "python3")
    original = ConnectionError("transport unavailable")
    def failed(*a, **kw):
        raise original
    monkeypatch.setattr(runner, "run_command", failed)
    with pytest.raises(ConnectionError) as caught:
        x11._helper_readable(runner, x11._HELPER_SCRIPT, "/helper.py", timeout=5)
    assert caught.value is original
    assert caught.value.helper_deployment["stage"] == "readability"
    assert runner.uploads == []


def test_legacy_deployment_keeps_runner_configured_timeouts(monkeypatch):
    calls = []
    class LegacyRunner:
        def run_command(self, command):
            calls.append(command)
            return SimpleNamespace(returncode=0, stderr="")
        def upload(self, local, remote):
            calls.append(remote)
            return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(x11, "default_virtuoso_bridge_dir", lambda *a: "/scratch/x11")
    path = x11._ensure_helper(LegacyRunner(), "user")
    assert path == "/scratch/x11/x11_dismiss_dialog.py"
    assert len(calls) == 3


@pytest.mark.parametrize("operation", ["find_dialogs", "list_windows", "dismiss_window",
                                       "bootstrap_ciw", "dismiss_dialogs"])
def test_legacy_wrappers_keep_list_error_shape_on_deployment_failure(monkeypatch, operation):
    monkeypatch.setattr(x11, "load_vb_env", lambda: None)
    runner = _FakeRunner()
    monkeypatch.setattr(runner, "upload", lambda *a, **k:
                        SimpleNamespace(returncode=1, stderr="upload failed"))
    monkeypatch.setattr(x11, "_run", lambda *a, **k: pytest.fail("must not execute helper"))
    args, kwargs = [], {}
    if operation == "dismiss_window":
        args = ["0x10"]
    elif operation == "bootstrap_ciw":
        args = ["0x10", "/setup.il"]
    elif operation == "dismiss_dialogs":
        kwargs = {"allow_legacy_bulk": True}
    result = getattr(x11, operation)(runner, "user", *args, **kwargs)
    assert isinstance(result, list) and len(result) == 1
    assert result[0]["error"] and result[0]["returncode"] == 1
    assert result[0]["helper_deployment"]["stage"] == "upload"


@pytest.mark.parametrize("stage", ["directory", "upload", "readability"])
@pytest.mark.parametrize("error_type", [ConnectionError, TimeoutError, FileNotFoundError])
def test_deployment_preserves_original_transport_exception(monkeypatch, stage, error_type):
    runner = _FakeRunner()
    original = error_type("transport unavailable")
    run = runner.run_command
    def command(text, timeout=None):
        if (stage == "directory" and text.startswith("mkdir")) or (
                stage == "readability" and text.startswith("test -r")):
            raise original
        return run(text, timeout)
    monkeypatch.setattr(runner, "run_command", command)
    if stage == "upload":
        def upload(*a, **kw):
            raise original
        monkeypatch.setattr(runner, "upload", upload)
    with pytest.raises(error_type) as caught:
        x11._deploy_helper(runner, x11._HELPER_SCRIPT, "/scratch/helper.py", timeout=5)
    assert caught.value is original
    assert original.helper_deployment["stage"] == stage


@pytest.mark.parametrize("cached", [False, True])
def test_diagnostics_do_not_mask_read_only_transport_error(monkeypatch, cached):
    class ReadOnlyDiagnostics(ConnectionError):
        @property
        def helper_deployment(self):
            return "owned by backend"
    original = ReadOnlyDiagnostics("transport unavailable")
    runner = _FakeRunner()
    def failed(*a, **kw):
        raise original
    monkeypatch.setattr(runner, "run_command", failed)
    with pytest.raises(ConnectionError) as caught:
        if cached:
            x11._helper_readable(runner, x11._HELPER_SCRIPT, "/helper.py", timeout=5)
        else:
            x11._deploy_helper(runner, x11._HELPER_SCRIPT, "/helper.py", timeout=5)
    assert caught.value is original
    assert original.helper_deployment == "owned by backend"

from __future__ import annotations

import io
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from virtuoso_bridge import cli
from virtuoso_bridge.virtuoso import x11


def _load_helper_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "virtuoso_bridge"
        / "resources"
        / "x11_dismiss_dialog.py"
    )
    spec = importlib.util.spec_from_file_location("x11_dismiss_dialog_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _xwininfo_window(*, x=0, y=0, w=100, h=100, mapped=True):
    state = "IsViewable" if mapped else "IsUnMapped"
    return f"""
xwininfo: Window id: 0x1

  Absolute upper-left X:  {x}
  Absolute upper-left Y:  {y}
  Width: {w}
  Height: {h}
  Map State: {state}
"""


def test_discover_windows_reports_child_modal_title(monkeypatch) -> None:
    helper = _load_helper_module()

    root = """
xwininfo: Window id: 0xroot (the root window)

  Root window id: 0xroot
  Parent window id: 0x0 (none)
     2 children:
     0xc58227 (has no name): () 843x132+528+477 +528+477
     0xabc000 "Virtuoso Main": ("virtuoso" "virtuoso") 1400x900+0+0 +0+0
"""
    ade_tree = """
xwininfo: Window id: 0xc58227 (has no name)

  Root window id: 0xroot
  Parent window id: 0xroot
     1 child:
     0x4203583 "ADE Explorer Update and Run": ("virtuoso" "virtuoso") 843x132+0+0 +528+477
"""
    main_tree = """
xwininfo: Window id: 0xabc000 "Virtuoso Main"

  Root window id: 0xroot
  Parent window id: 0xroot
     1 child:
     0xabc111 "Virtuoso Schematic Editor": ("virtuoso" "virtuoso") 1400x900+0+0 +0+0
"""

    def fake_check_output(cmd, stderr=None):
        if cmd == ["xwininfo", "-root", "-children"]:
            return root.encode()
        if cmd == ["xwininfo", "-id", "0xc58227"]:
            return _xwininfo_window(x=528, y=477, w=843, h=132).encode()
        if cmd == ["xwininfo", "-id", "0xabc000"]:
            return _xwininfo_window(x=0, y=0, w=1400, h=900).encode()
        if cmd == ["xwininfo", "-id", "0xc58227", "-tree"]:
            return ade_tree.encode()
        if cmd == ["xwininfo", "-id", "0xabc000", "-tree"]:
            return main_tree.encode()
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(helper.subprocess, "check_output", fake_check_output)

    windows = helper.discover_windows(":1")
    ade = next(w for w in windows if w["dismiss_id"] == "0x4203583")
    main = next(w for w in windows if w["dismiss_id"] == "0xabc111")

    assert ade["frame_id"] == "0xc58227"
    assert ade["title"] == "ADE Explorer Update and Run"
    assert ade["kind"] == "known_modal"
    assert ade["suggested_action"] == "enter"
    assert ade["geometry"] == {"w": 843, "h": 132, "x": 528, "y": 477}
    assert main["kind"] == "main_window"
    assert main["suggested_action"] is None

    dialogs = helper.find_dialogs(":1")
    assert [d["window_id"] for d in dialogs] == ["0x4203583"]


def test_assembler_1749_uses_the_ok_mnemonic() -> None:
    helper = _load_helper_module()

    assert helper._known_action("ADE Assembler Message 1749") == "alt-o"


def test_find_x11_env_decodes_pgrep_pid_bytes(monkeypatch) -> None:
    helper = _load_helper_module()
    opened_paths = []

    def fake_check_output(cmd, stderr=None):
        assert cmd == ["pgrep", "-u", "designer", "-x", "virtuoso"]
        return b"123\n"

    def fake_open(path, mode="r"):
        opened_paths.append(path)
        if path == "/proc/123/cmdline":
            return io.BytesIO(b"virtuoso\x00")
        if path == "/proc/123/environ":
            return io.BytesIO(b"DISPLAY=:7\x00XAUTHORITY=/tmp/xauth\x00")
        raise AssertionError(f"unexpected path: {path!r}")

    monkeypatch.setattr(helper.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(helper, "open", fake_open, raising=False)

    env = helper.find_x11_env(user="designer")

    assert env == {"DISPLAY": ":7", "XAUTHORITY": "/tmp/xauth"}
    assert opened_paths == ["/proc/123/cmdline", "/proc/123/environ"]
    assert not any("b'123'" in path for path in opened_paths)


class _Runner:
    def __init__(self, stdout_by_marker: dict[str, str]) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[Path, str]] = []
        self.stdout_by_marker = stdout_by_marker

    def run_command(self, command: str, timeout=None):
        self.commands.append(command)
        if command.startswith("mkdir -p "):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "python3 --version" in command:
            return SimpleNamespace(returncode=0, stdout='Python 3.9\nCMD:python3\n', stderr="")
        for marker, stdout in self.stdout_by_marker.items():
            if marker in command:
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def upload(self, local_path: Path, remote_path: str):
        self.uploads.append((local_path, remote_path))


def test_x11_wrapper_lists_and_dismisses_explicit_window(monkeypatch) -> None:
    monkeypatch.setattr(x11, "load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.delenv("VB_REMOTE_SCRATCH_ROOT", raising=False)
    monkeypatch.setenv("VB_CLIENT_ID", "90590")
    runner = _Runner({
        "--list-windows": '{"dismiss_id":"0x4203583","title":"ADE Explorer Update and Run"}\n',
        "--dismiss-window": '{"dismissed":"0x4203583","action":"enter"}\n',
    })

    windows = x11.list_windows(runner, "designer", profile=None)
    result = x11.dismiss_window(runner, "designer", "0x4203583", action="enter")

    assert windows == [{"dismiss_id": "0x4203583", "title": "ADE Explorer Update and Run"}]
    assert result == [{"dismissed": "0x4203583", "action": "enter"}]
    assert any("--list-windows --json :".split()[0] in cmd for cmd in runner.commands)
    assert any("--dismiss-window 0x4203583 --action enter" in cmd for cmd in runner.commands)


def test_make_ssh_runner_skips_ssh_for_localhost(monkeypatch) -> None:
    def fail_if_instantiated(*args, **kwargs):
        raise AssertionError("local X11 commands should not instantiate SSHRunner")

    monkeypatch.setattr(cli, "_CLI_PROFILE", [None])
    monkeypatch.setenv("VB_REMOTE_HOST", "localhost")
    monkeypatch.setenv("VB_REMOTE_USER", "designer")
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.SSHRunner", fail_if_instantiated)

    runner, user = cli._make_ssh_runner()

    assert runner is None
    assert user == "designer"


def test_make_ssh_runner_uses_profile_backend_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _CapturedRunner:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(cli, "_CLI_PROFILE", ["worker"])
    monkeypatch.setenv("VB_REMOTE_HOST_worker", "compute")
    monkeypatch.setenv("VB_REMOTE_USER_worker", "designer")
    monkeypatch.setenv("VB_SSH_BACKEND_worker", "paramiko")
    monkeypatch.setenv("VB_SSH_MAX_SESSIONS_worker", "255")
    monkeypatch.setenv("VB_SSH_PROXY_worker", "socks5://127.0.0.1:10800")
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.load_vb_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.transport.ssh.SSHRunner", _CapturedRunner)

    runner, user = cli._make_ssh_runner()

    assert runner is not None
    assert user == "designer"
    assert captured["backend"] == "paramiko"
    assert captured["max_sessions"] == 255
    assert captured["proxy_url"] == "socks5://127.0.0.1:10800"


def test_helper_exports_auto_detected_display(monkeypatch, capsys) -> None:
    helper = _load_helper_module()
    calls = []

    def fake_dismiss_window(display, win_id, *args, **kwargs):
        calls.append((display, win_id, helper.os.environ.get("DISPLAY")))
        return {"dismissed": win_id}

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(
        helper,
        "find_x11_envs",
        lambda: [{"DISPLAY": ":7", "XAUTHORITY": ""}],
    )
    monkeypatch.setattr(
        helper,
        "discover_windows",
        lambda _display: [{"frame_id": "0xframe", "dismiss_id": "0xabc"}],
    )
    monkeypatch.setattr(helper, "_verify_dismissal", lambda result: result)
    monkeypatch.setattr(helper, "dismiss_window", fake_dismiss_window)
    monkeypatch.setattr(helper.sys, "argv", ["x11_dismiss_dialog.py", "--dismiss-window", "0xabc"])

    try:
        helper.main()
    except SystemExit as exc:
        assert exc.code == 0

    assert calls == [(":7", "0xabc", ":7")]
    assert helper.os.environ["DISPLAY"] == ":7"
    out = capsys.readouterr().out
    assert '"dismissed": "0xabc"' in out

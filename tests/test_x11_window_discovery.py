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


def test_top_level_discovery_returns_one_verified_ciw_per_frame(monkeypatch) -> None:
    helper = _load_helper_module()
    root = """
     1 child:
     0xf00 (has no name): () 1200x800+0+0 +0+0
"""
    children = """
     3 children:
     0xc10 "Virtuoso Command Interpreter Window": ("virtuoso" "Virtuoso") 1200x800+0+0 +0+0
     0xd01 "Virtuoso Command Interpreter Window": ("virtuoso" "Virtuoso") 1200x800+0+0 +0+0
     0xd02 "Virtuoso Command Interpreter Window": ("virtuoso" "Virtuoso") 1200x800+0+0 +0+0
"""

    def fake_check_output(cmd, stderr=None):
        if cmd == ["xwininfo", "-root", "-children"]:
            return root.encode()
        if cmd == ["xwininfo", "-id", "0xf00"]:
            return _xwininfo_window(w=1200, h=800).encode()
        if cmd == ["xwininfo", "-id", "0xf00", "-children"]:
            return children.encode()
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(helper.subprocess, "check_output", fake_check_output)

    windows = helper.discover_windows(":7", top_level=True)

    assert len(windows) == 1
    assert windows[0]["frame_id"] == "0xf00"
    assert windows[0]["dismiss_id"] == "0xc10"
    assert windows[0]["kind"] == "ciw"


def test_top_level_discovery_repairs_locale_damaged_ciw_title(monkeypatch) -> None:
    helper = _load_helper_module()
    root = r'''
     1 child:
     0xf00 " (failure in conversion from UTF8_STRING to ANSI_X3.4-1968)": ("virtuoso" "virtuoso") 795x193+0+0 +0+0
'''
    xprop = r'''
_NET_WM_NAME(UTF8_STRING) = "Virtuoso\302\256 6.1.8-64b - Log: /tmp/virtuoso.log"
WM_NAME(STRING) = "Virtuoso 6.1.8-64b - Log: /tmp/virtuoso.log"
WM_CLASS(STRING) = "virtuoso", "virtuoso"
'''

    def fake_check_output(cmd, stderr=None):
        if cmd == ["xwininfo", "-root", "-children"]:
            return root.encode()
        if cmd == ["xprop", "-id", "0xf00", "_NET_WM_NAME", "WM_NAME", "WM_CLASS"]:
            return xprop.encode()
        if cmd == ["xwininfo", "-id", "0xf00"]:
            return _xwininfo_window(w=795, h=193).encode()
        if cmd == ["xwininfo", "-id", "0xf00", "-children"]:
            return b"0 children:\n"
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(helper.subprocess, "check_output", fake_check_output)

    windows = helper.discover_windows(":7020", top_level=True)

    assert len(windows) == 1
    assert windows[0]["kind"] == "ciw"
    assert windows[0]["title"] == "Virtuoso 6.1.8-64b - Log: /tmp/virtuoso.log"


def test_bootstrap_refuses_non_ciw_and_injects_only_generated_load(monkeypatch) -> None:
    helper = _load_helper_module()
    typed = []

    monkeypatch.setattr(
        helper,
        "discover_windows",
        lambda _display, top_level=False: [{
            "frame_id": "0xframe",
            "window_id": "0xchild",
            "dismiss_id": "0xchild",
            "title": "Virtuoso Schematic Editor",
            "kind": "main_window",
        }],
    )
    refused = helper.bootstrap_ciw(":7", "0xframe", "/shared/virtuoso_setup.il")
    assert "refusing bootstrap" in refused["error"]

    monkeypatch.setattr(
        helper,
        "discover_windows",
        lambda _display, top_level=False: [{
            "frame_id": "0xframe",
            "window_id": "0xciw",
            "dismiss_id": "0xciw",
            "title": "Virtuoso Command Interpreter Window",
            "kind": "ciw",
        }],
    )
    monkeypatch.setattr(
        helper,
        "_type_ascii_into_window",
        lambda display, window, text: typed.append((display, window, text)) or {"bootstrapped": window},
    )

    result = helper.bootstrap_ciw(":7", "0xframe", "/shared/virtuoso_setup.il")

    assert "error" not in result
    assert typed == [(":7", "0xciw", 'load("/shared/virtuoso_setup.il")')]


def test_bootstrap_does_not_inject_when_window_id_matches_two_displays(monkeypatch, capsys) -> None:
    helper = _load_helper_module()
    monkeypatch.setattr(
        helper,
        "find_x11_envs",
        lambda: [
            {"DISPLAY": ":7", "XAUTHORITY": "/tmp/a"},
            {"DISPLAY": ":8", "XAUTHORITY": "/tmp/b"},
        ],
    )
    monkeypatch.setattr(
        helper,
        "discover_windows",
        lambda _display, top_level=False: [{
            "frame_id": "0xabc",
            "window_id": "0xdef",
            "dismiss_id": "0xdef",
            "title": "Virtuoso Command Interpreter Window",
            "kind": "ciw",
        }],
    )
    monkeypatch.setattr(
        helper,
        "bootstrap_ciw",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must validate uniqueness before injection")
        ),
    )
    monkeypatch.setattr(
        helper.sys,
        "argv",
        [
            "x11_dismiss_dialog.py",
            "--bootstrap-window",
            "0xabc",
            "--setup-path",
            "/shared/virtuoso_setup.il",
        ],
    )

    try:
        helper.main()
    except SystemExit as exc:
        assert exc.code == 1

    assert "more than one display" in capsys.readouterr().out


def test_x11_wrapper_requests_top_level_mode(monkeypatch) -> None:
    monkeypatch.setattr(x11, "load_vb_env", lambda: None)
    runner = _Runner({"--list-windows": '{"kind":"ciw"}\n'})

    windows = x11.list_windows(runner, "designer", top_level=True)

    assert windows == [{"kind": "ciw"}]
    assert any("--list-windows --json --top-level" in cmd for cmd in runner.commands)


def test_x11_wrapper_prioritizes_selected_bootstrap_result(monkeypatch) -> None:
    monkeypatch.setattr(x11, "load_vb_env", lambda: None)
    runner = _Runner({
        "--bootstrap-window": (
            '{"error":"xwininfo failed on stale display"}\n'
            '{"bootstrapped":"0xciw","requested_window_id":"0xframe","display":":7020"}\n'
        ),
    })

    results = x11.bootstrap_ciw(
        runner,
        "designer",
        "0xframe",
        "/shared/virtuoso_setup.il",
    )

    assert results[0]["bootstrapped"] == "0xciw"
    assert results[1]["error"] == "xwininfo failed on stale display"


def test_cli_bootstrap_uses_daemon_token_for_health_check(monkeypatch, capsys) -> None:
    token = "a" * 64
    seen: dict[str, object] = {}

    class _FakeBridge:
        port = 65061
        daemon_host = "thu-wei"

        def ensure_daemon_token(self):
            return token

        def close(self):
            seen["closed"] = True

    class _FakeSSHClient:
        @staticmethod
        def read_state(profile=None):
            assert profile == "tsmc"
            return {"setup_path": "/shared/virtuoso_setup.il", "port": 65061}

        @classmethod
        def from_env(cls, **_kwargs):
            return _FakeBridge()

    class _FakeVirtuosoClient:
        def __init__(self, *, host, port, timeout, daemon_token):
            seen.update(host=host, port=port, timeout=timeout, daemon_token=daemon_token)

        def test_connection(self, timeout=1):
            seen["probe_timeout"] = timeout
            return True

    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_CLI_PROFILE", ["tsmc"])
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (object(), "designer"))
    monkeypatch.setattr("virtuoso_bridge.transport.tunnel.SSHClient", _FakeSSHClient)
    monkeypatch.setattr(
        "virtuoso_bridge.virtuoso.x11.bootstrap_ciw",
        lambda *_args, **_kwargs: [{
            "bootstrapped": "0xciw",
            "requested_window_id": "0xframe",
            "command": 'load("/shared/virtuoso_setup.il")',
        }],
    )
    monkeypatch.setattr(
        "virtuoso_bridge.virtuoso.basic.bridge.VirtuosoClient",
        _FakeVirtuosoClient,
    )

    rc = cli.cli_bootstrap(window_id="0xframe", timeout=8)

    assert rc == 0
    assert seen["daemon_token"] == token
    assert seen["timeout"] == 5
    assert seen["probe_timeout"] == 5
    assert seen["closed"] is True
    assert "[daemon] OK" in capsys.readouterr().out


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

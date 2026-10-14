"""Explicit-display X11 actions use the Xauthority cookie of the Virtuoso process on that display.

xvfb-run / cdsXvfb-run start each X server with its own cookie file, named only in the
environment of the processes it launches; the SSH shell that runs the helper has none.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from virtuoso_bridge import cli
from virtuoso_bridge.virtuoso import x11

HELPER = Path(__file__).resolve().parents[1] / "src" / "virtuoso_bridge" / "resources" / "x11_dismiss_dialog.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("x11_dismiss_dialog_explicit_display", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(helper, monkeypatch, argv):
    monkeypatch.setattr(helper.sys, "argv", ["x11_dismiss_dialog.py", *argv])
    with pytest.raises(SystemExit) as exc:
        helper.main()
    return exc.value.code


@pytest.fixture
def helper(monkeypatch):
    module = _load_helper()
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(module, "_SHELL_XAUTHORITY", None)
    monkeypatch.setattr(module, "_verify_dismissal", lambda result: dict(
        result, status="closed", closure_confirmed=True, still_mapped=False))
    return module


def _record_x11(helper, monkeypatch, windows_by_display):
    seen, dismissed = [], []

    def discover(display, top_level=False):
        seen.append((display, helper.os.environ.get("DISPLAY"), helper.os.environ.get("XAUTHORITY")))
        return windows_by_display.get(display, [])

    def dismiss(display, win_id, *args, **kwargs):
        dismissed.append((display, win_id, helper.os.environ.get("XAUTHORITY")))
        return {"dismissed": win_id}

    monkeypatch.setattr(helper, "discover_windows", discover)
    monkeypatch.setattr(helper, "dismiss_window", dismiss)
    return seen, dismissed


def test_explicit_display_uses_the_cookie_of_the_virtuoso_on_that_display(helper, monkeypatch, capsys):
    monkeypatch.setattr(helper, "find_x11_envs", lambda: [
        {"DISPLAY": ":5", "XAUTHORITY": "/tmp/xvfb-5/Xauthority"},
        {"DISPLAY": ":7", "XAUTHORITY": "/tmp/xvfb-7/Xauthority"},
    ])
    seen, dismissed = _record_x11(helper, monkeypatch, {":7": [{"frame_id": "0xf", "dismiss_id": "0xabc"}]})

    assert _run_main(helper, monkeypatch, ["--dismiss-window", "0xabc", "--action", "escape", ":7"]) == 0

    assert {display for display, _, _ in seen} == {":7"}
    assert all(xauth == "/tmp/xvfb-7/Xauthority" for _, _, xauth in seen)
    assert dismissed == [(":7", "0xabc", "/tmp/xvfb-7/Xauthority")]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["display"] == ":7"


def test_explicit_display_never_falls_through_to_another_display(helper, monkeypatch, capsys):
    # The same window id exists on the user's own :5; the request names :7, which has no such window.
    monkeypatch.setattr(helper, "find_x11_envs", lambda: [{"DISPLAY": ":5", "XAUTHORITY": "/tmp/xvfb-5/Xauthority"}])
    seen, dismissed = _record_x11(helper, monkeypatch, {":5": [{"frame_id": "0xf", "dismiss_id": "0xabc"}]})

    assert _run_main(helper, monkeypatch, ["--dismiss-window", "0xabc", ":7"]) == 1

    assert {display for display, _, _ in seen} == {":7"}
    assert all(xauth is None for _, _, xauth in seen)            # :5's cookie is not borrowed
    assert dismissed == []
    assert "window id not found" in capsys.readouterr().out


def test_explicit_display_falls_back_to_the_shell_cookie(helper, monkeypatch):
    monkeypatch.setattr(helper, "_SHELL_XAUTHORITY", "/home/designer/.Xauthority")
    monkeypatch.setattr(helper, "find_x11_envs", lambda: [{"DISPLAY": ":7", "XAUTHORITY": None}])
    seen, dismissed = _record_x11(helper, monkeypatch, {":7": [{"frame_id": "0xf", "dismiss_id": "0xabc"}]})

    assert _run_main(helper, monkeypatch, ["--dismiss-window", "0xabc", ":7"]) == 0

    assert dismissed == [(":7", "0xabc", "/home/designer/.Xauthority")]


def test_display_names_compare_by_server_not_spelling(helper, monkeypatch):
    monkeypatch.setattr(helper, "find_x11_envs", lambda: [
        {"DISPLAY": "unix:7", "XAUTHORITY": "/tmp/xvfb-7/Xauthority"},
        {"DISPLAY": "localhost:7.0", "XAUTHORITY": "/tmp/forwarded"},
    ])

    assert helper._explicit_display_env(":7.0") == {"DISPLAY": ":7.0", "XAUTHORITY": "/tmp/xvfb-7/Xauthority"}
    assert helper._explicit_display_env("localhost:7") == {"DISPLAY": "localhost:7", "XAUTHORITY": "/tmp/forwarded"}
    assert helper._display_key("bogus") is None
    assert helper._same_display("bogus", "bogus")


def test_several_cookies_for_one_display_keep_the_one_that_connects(helper, monkeypatch):
    monkeypatch.setattr(helper, "find_x11_envs", lambda: [
        {"DISPLAY": ":7", "XAUTHORITY": "/tmp/stale"},
        {"DISPLAY": ":7", "XAUTHORITY": "/tmp/xvfb-7/Xauthority"},
    ])
    probed = []
    monkeypatch.setattr(helper, "_xauthority_connects",
                        lambda display, xauth: probed.append(xauth) or xauth == "/tmp/xvfb-7/Xauthority")

    assert helper._explicit_display_env(":7")["XAUTHORITY"] == "/tmp/xvfb-7/Xauthority"
    assert probed == ["/tmp/stale", "/tmp/xvfb-7/Xauthority"]


def test_pid_pins_display_and_cookie_to_one_process(helper, monkeypatch):
    def no_enumeration():
        raise AssertionError("--pid must not enumerate other Virtuoso processes")

    monkeypatch.setattr(helper, "find_x11_envs", no_enumeration)
    monkeypatch.setattr(helper, "_read_process_x11_env",
                        lambda pid: {"DISPLAY": ":9", "XAUTHORITY": "/tmp/xvfb-9/Xauthority", "HOSTNAME": None}
                        if pid == 4242 else pytest.fail("wrong pid %r" % pid))
    seen, dismissed = _record_x11(helper, monkeypatch, {":9": [{"frame_id": "0xf", "dismiss_id": "0xabc"}]})

    assert _run_main(helper, monkeypatch, ["--dismiss-window", "0xabc", "--pid", "4242", ":9.0"]) == 0

    assert dismissed == [(":9", "0xabc", "/tmp/xvfb-9/Xauthority")]


@pytest.mark.parametrize("argv, message", [
    (["--dismiss-window", "0xabc", "--pid", "4242", ":5"], "disagrees with process DISPLAY :9"),
    (["--dismiss-window", "0xabc", "--pid", "-3"], "--pid requires a positive integer"),
])
def test_pid_refusals_send_nothing(helper, monkeypatch, capsys, argv, message):
    monkeypatch.setattr(helper, "_read_process_x11_env",
                        lambda pid: {"DISPLAY": ":9", "XAUTHORITY": "/tmp/x9", "HOSTNAME": None})
    seen, dismissed = _record_x11(helper, monkeypatch, {})

    assert _run_main(helper, monkeypatch, argv) == 2

    assert seen == [] and dismissed == []
    assert message in json.loads(capsys.readouterr().out)["error"]


def test_auto_detected_displays_do_not_inherit_a_previous_cookie(helper, monkeypatch):
    monkeypatch.setattr(helper, "find_x11_envs", lambda: [
        {"DISPLAY": ":5", "XAUTHORITY": "/tmp/xvfb-5/Xauthority"},
        {"DISPLAY": ":7", "XAUTHORITY": None},
    ])
    seen, _ = _record_x11(helper, monkeypatch, {})

    _run_main(helper, monkeypatch, ["--list-windows"])

    assert [(display, xauth) for display, _, xauth in seen] == [(":5", "/tmp/xvfb-5/Xauthority"), (":7", None)]


def test_client_and_cli_pass_pid_through(monkeypatch, capsys):
    commands = []
    monkeypatch.setattr(x11, "load_vb_env", lambda: None)
    monkeypatch.setattr(x11, "_ensure_helper", lambda *a: "/helper.py")
    monkeypatch.setattr(x11, "_detect_remote_python", lambda *a: "python3")
    monkeypatch.setattr(x11, "_run", lambda runner, command, **kw: commands.append(command)
                        or type("R", (), {"returncode": 0, "stdout": '{"dismissed": "0x10"}\n', "stderr": ""})())
    x11.dismiss_window(None, "local", "0x10", display=":7", pid=4242)
    assert commands[-1] == "python3 /helper.py --dismiss-window 0x10 --action enter --pid 4242 :7"
    for bad in (0, -1, True, "42"):
        with pytest.raises(ValueError):
            x11.dismiss_window(None, "local", "0x10", pid=bad)

    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (None, "local"))
    seen = []
    monkeypatch.setattr(x11, "dismiss_window", lambda *a, **kw: seen.append(kw) or [
        {"dismissed": "0x10", "status": "closed", "closure_confirmed": True, "still_mapped": False}])
    assert cli.main(["dismiss-window", "0x10", "--display", ":7", "--pid", "4242", "--action", "escape"]) == 0
    assert (seen[-1]["display"], seen[-1]["pid"], seen[-1]["action"]) == (":7", 4242, "escape")
    assert cli.main(["dismiss-window", "0x10", "--pid", "0"]) == 2
    assert len(seen) == 1
    assert "--pid requires a positive integer" in capsys.readouterr().out


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<pid>/environ")
def test_helper_process_passes_the_display_cookie_to_xwininfo(tmp_path):
    """Run the real helper: real /proc environments, fake pgrep/xwininfo, bounded by a timeout."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_virtuoso = bindir / "virtuoso"
    fake_virtuoso.symlink_to(sys.executable)
    sleeper = ["-c", "import time; time.sleep(60)"]
    base = {k: v for k, v in os.environ.items() if k not in {"DISPLAY", "XAUTHORITY"}}
    procs = [
        subprocess.Popen([str(fake_virtuoso), *sleeper], env={**base, "DISPLAY": display, "XAUTHORITY": cookie})
        for display, cookie in ((":5", str(tmp_path / "cookie5")), (":7", str(tmp_path / "cookie7")))
    ]
    log = tmp_path / "xwininfo.log"
    try:
        (bindir / "pgrep").write_text("#!/bin/sh\n" + "".join(f"echo {p.pid}\n" for p in procs))
        (bindir / "xwininfo").write_text(
            f'#!/bin/sh\necho "DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY $*" >> {log}\nexit 1\n')
        for name in ("pgrep", "xwininfo"):
            (bindir / name).chmod(0o755)
        env = {**base, "PATH": f"{bindir}{os.pathsep}{base.get('PATH', '')}", "USER": "designer"}

        by_display = subprocess.run([sys.executable, str(HELPER), "--dismiss-window", "0xabc", ":7"],
                                    env=env, capture_output=True, text=True, timeout=30)
        by_pid = subprocess.run([sys.executable, str(HELPER), "--dismiss-window", "0xabc",
                                 "--pid", str(procs[1].pid)], env=env, capture_output=True, text=True, timeout=30)
    finally:
        for p in procs:
            p.kill()
            p.wait()

    assert by_display.returncode == 1 and by_pid.returncode == 1          # no such window: nothing sent
    assert "window id not found" in by_display.stdout and "window id not found" in by_pid.stdout
    lines = log.read_text().splitlines()
    assert lines, by_display.stderr + by_pid.stderr
    assert all(line.startswith(f"DISPLAY=:7 XAUTHORITY={tmp_path / 'cookie7'} ") for line in lines), lines

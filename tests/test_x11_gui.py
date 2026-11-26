import os
import shlex
import shutil
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.x11_gui import GuiActionError, GuiWindow, X11Gui

WMCTRL = (
    "0x02e00010  0 host Virtuoso® Studio IC23.1 - Log: /home/u/CDS.log\n"
    "0x02e1ec84  0 host Virtuoso® ADE Assembler Editing: SAR_ADC _tb_cmp_strongarm maestro*\n"
    "0x02e1ee78  0 host Virtuoso® ADE Assembler Editing: SAR_ADC _tb_cmp_miyahara maestro\n"
    "0x02e1ee09  0 host Choosing Analyses -- ADE Assembler\n"
)
XWININFO = {
    "0x02e1ee09": ("  Absolute upper-left X:  410\n  Absolute upper-left Y:  195\n"
                   "  Relative upper-left X:  0\n  Width: 415\n  Height: 982\n"),
}


class FakeRunner:
    def __init__(self):
        self.commands = []
        self.downloads = []

    def run_command(self, command, timeout=None):
        assert command.startswith("bash -c "), command
        script = shlex.split(command)[2]
        self.commands.append(script)
        out = ""
        if "wmctrl -l" in script and "-a" not in script and "-r" not in script:
            out = WMCTRL
        elif "xwininfo -id" in script:
            out = XWININFO.get(script.split()[-1], "  Absolute upper-left X:  0\n"
                               "  Absolute upper-left Y:  0\n  Width: 100\n  Height: 100\n")
        elif "command -v" in script:
            out = "xdotool\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def download(self, remote, local, timeout=None):
        self.downloads.append((remote, local))
        local.write_bytes(b"png")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def gui():
    runner = FakeRunner()
    return X11Gui(runner, "u", display=":1"), runner


def test_find_requires_exactly_one_match(gui):
    g, _ = gui
    assert g.find("Choosing Analyses").window_id == "0x02e1ee09"
    with pytest.raises(GuiActionError, match="exactly one"):
        g.find("ADE Assembler Editing")
    with pytest.raises(GuiActionError, match="none"):
        g.find("no such window")


def test_click_translates_window_coordinates_and_sets_display(gui):
    g, runner = gui
    rec = g.click("Choosing Analyses", 84, 58, count=2)
    assert rec["root"] == [494, 253]
    click = [c for c in runner.commands if "mousemove" in c][0]
    assert click.startswith("export DISPLAY=:1;")
    assert "mousemove --sync 494 253 click --repeat 2" in click
    assert any("wmctrl -i -a 0x02e1ee09" in c for c in runner.commands)


def test_point_outside_window_refuses_before_any_input(gui):
    g, runner = gui
    with pytest.raises(GuiActionError, match="outside window"):
        g.click("Choosing Analyses", 415, 10)
    assert not any("xdotool mousemove" in c or "wmctrl -i -a" in c for c in runner.commands)


def test_set_text_quotes_skill_expression(gui):
    g, runner = gui
    g.set_text("Choosing Analyses", 346, 338, '0.5/VAR("tclk")')
    typed = [c for c in runner.commands if "xdotool type" in c][0]
    assert "xdotool type --delay 30 -- '0.5/VAR(\"tclk\")'" in typed
    assert typed.index("ctrl+a") < typed.index("xdotool type") < typed.index("Tab")


def test_screenshot_crops_to_window_client_area(gui, tmp_path):
    g, runner = gui
    out = g.screenshot(tmp_path / "f.png", title="Choosing Analyses", scale=0.5)
    shot = [c for c in runner.commands if "import -window root" in c][0]
    assert "-crop 415x982+410+195 +repage" in shot
    assert "-resize 50%" in shot
    assert out.read_bytes() == b"png"
    assert any(c.startswith("rm -f ") for c in runner.commands)


def test_screenshot_region_is_relative_to_window(gui, tmp_path):
    g, runner = gui
    g.screenshot(tmp_path, title="Choosing Analyses", region=(10, 20, 100, 50))
    shot = [c for c in runner.commands if "import -window root" in c][0]
    assert "-crop 100x50+420+215 +repage" in shot


def test_key_requires_keys(gui):
    g, _ = gui
    with pytest.raises(GuiActionError):
        g.key("Choosing Analyses")


def test_missing_tools(gui):
    g, _ = gui
    assert g.missing_tools() == ["xdotool"]


def test_window_to_root_bounds():
    w = GuiWindow("0x1", "t", 10, 20, 30, 40)
    assert w.to_root(0, 0) == (10, 20)
    assert w.to_root(29, 39) == (39, 59)
    with pytest.raises(GuiActionError):
        w.to_root(30, 0)


def test_actions_chain_with_and_and_errexit(gui):
    g, runner = gui
    g.pin("Choosing Analyses", 10, 20, 400, 500)
    g.click("Choosing Analyses", 10, 20)
    g.set_text("Choosing Analyses", 10, 20, "hello")
    g.key("Choosing Analyses", "Return")

    action_cmds = [
        c for c in runner.commands
        if any(tok in c for tok in ("wmctrl -i -a", "wmctrl -i -r", "xdotool "))
    ]
    assert action_cmds
    for cmd in action_cmds:
        assert "; set -e; " in cmd
        body = cmd.split("; set -e; ", 1)[1]
        assert ";" not in body
        assert "&& sleep" in body


def test_screenshot_formats_negative_multimonitor_crop_offsets(gui, tmp_path):
    g, runner = gui
    XWININFO_NEG = (
        "  Absolute upper-left X:  -10\n  Absolute upper-left Y:  20\n"
        "  Relative upper-left X:  0\n  Width: 100\n  Height: 50\n"
    )
    orig_run = runner.run_command

    def neg_run(command, timeout=None):
        res = orig_run(command, timeout=timeout)
        script = shlex.split(command)[2]
        if "xwininfo -id" in script:
            return SimpleNamespace(returncode=0, stdout=XWININFO_NEG, stderr="")
        return res

    runner.run_command = neg_run

    g.screenshot(tmp_path / "neg.png", title="Choosing Analyses")
    shot = [c for c in runner.commands if "import -window root" in c][-1]
    assert "-crop 100x50-10+20 +repage" in shot
    assert "+-" not in shot

    g.screenshot(tmp_path / "both_neg.png", region=(-10, -20, 100, 50))
    shot2 = [c for c in runner.commands if "import -window root" in c][-1]
    assert "-crop 100x50-10-20 +repage" in shot2
    assert "+-" not in shot2


def test_validation_rejects_malformed_window_id_settle_and_geometry(gui, tmp_path):
    g, runner = gui
    bad_window = GuiWindow("0x1; touch /tmp/should_not_run", "bad", 0, 0, 100, 100)
    with pytest.raises(GuiActionError, match="invalid X11 window id"):
        g.raise_window(bad_window)
    with pytest.raises(GuiActionError, match="invalid X11 window id"):
        g._geometry("0x1; id", "bad")

    for bad_settle in (-1, float("nan"), float("inf"), "0.5; id"):
        with pytest.raises(GuiActionError, match="invalid settle"):
            g.click("Choosing Analyses", 10, 10, settle=bad_settle)

    with pytest.raises(ValueError):
        g.pin("Choosing Analyses", "10; id", 0, 100, 100)

    for bad_scale in (0, -0.5, float("nan"), float("inf")):
        with pytest.raises(GuiActionError, match="invalid scale"):
            g.screenshot(tmp_path / "s.png", scale=bad_scale)

    assert not any("wmctrl -i -a" in c or "wmctrl -i -r" in c or "xdotool" in c for c in runner.commands)


def _install_fake_x11_bins(tmp_path, *, wmctrl_raise_rc=0, wmctrl_pin_rc=0, xdotool_fail_on=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    trace = tmp_path / "trace.log"

    wmctrl = bin_dir / "wmctrl"
    wmctrl.write_text(
        "#!/bin/sh\n"
        f'printf "wmctrl %s\\n" "$*" >> {shlex.quote(str(trace))}\n'
        'case "$*" in\n'
        '  *-l*)\n'
        '    printf "0x02e1ee09  0 host Choosing Analyses -- ADE Assembler\\n"\n'
        '    exit 0\n'
        '    ;;\n'
        '  *"-i -a"*)\n'
        f'    echo "wmctrl activate failed" >&2; exit {int(wmctrl_raise_rc)}\n'
        '    ;;\n'
        '  *"-i -r"*)\n'
        f'    echo "wmctrl pin failed" >&2; exit {int(wmctrl_pin_rc)}\n'
        '    ;;\n'
        'esac\n'
        'exit 0\n',
        encoding="utf-8",
    )
    wmctrl.chmod(0o755)

    xwininfo = bin_dir / "xwininfo"
    xwininfo.write_text(
        "#!/bin/sh\n"
        'printf "  Absolute upper-left X:  410\\n  Absolute upper-left Y:  195\\n"\n'
        'printf "  Width: 415\\n  Height: 982\\n"\n',
        encoding="utf-8",
    )
    xwininfo.chmod(0o755)

    fail_case = ""
    if xdotool_fail_on:
        fail_case = (
            f'case "$*" in\n'
            f'  *{xdotool_fail_on}*) echo "xdotool failed" >&2; exit 1 ;;\n'
            f'esac\n'
        )
    xdotool = bin_dir / "xdotool"
    xdotool.write_text(
        "#!/bin/sh\n"
        f'printf "xdotool %s\\n" "$*" >> {shlex.quote(str(trace))}\n'
        f"{fail_case}"
        "exit 0\n",
        encoding="utf-8",
    )
    xdotool.chmod(0o755)
    return bin_dir, trace


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="requires POSIX bash")
def test_real_bash_failing_wmctrl_raises_and_blocks_subsequent_input(tmp_path, monkeypatch):
    bin_dir, trace = _install_fake_x11_bins(tmp_path, wmctrl_raise_rc=1, wmctrl_pin_rc=1)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    g = X11Gui(None, "u", display=":99", timeout=10)

    with pytest.raises(GuiActionError, match="raise .* failed"):
        g.click("Choosing Analyses", 10, 20, settle=0.01)
    with pytest.raises(GuiActionError, match="raise .* failed"):
        g.set_text("Choosing Analyses", 10, 20, "secret", settle=0.01)
    with pytest.raises(GuiActionError, match="raise .* failed"):
        g.key("Choosing Analyses", "Return", settle=0.01)
    with pytest.raises(GuiActionError, match="pin .* failed"):
        g.pin("Choosing Analyses", 10, 20, 400, 500)

    lines = trace.read_text(encoding="utf-8").splitlines()
    assert any("wmctrl -i -a 0x02e1ee09" in line for line in lines)
    assert not any(line.startswith("xdotool ") for line in lines)


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="requires POSIX bash")
def test_real_bash_failing_xdotool_raises_and_stops_remaining_steps(tmp_path, monkeypatch):
    bin_dir, trace = _install_fake_x11_bins(tmp_path, wmctrl_raise_rc=0, xdotool_fail_on="mousemove")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    g = X11Gui(None, "u", display=":99", timeout=10)

    with pytest.raises(GuiActionError, match="type into .* failed"):
        g.set_text("Choosing Analyses", 10, 20, "secret", settle=0.01)
    with pytest.raises(GuiActionError, match="click in .* failed"):
        g.click("Choosing Analyses", 10, 20, settle=0.01)

    lines = trace.read_text(encoding="utf-8").splitlines()
    xdotool_lines = [line for line in lines if line.startswith("xdotool ")]
    assert len(xdotool_lines) == 2
    assert all("mousemove" in line for line in xdotool_lines)
    assert not any("ctrl+a" in line or "type" in line or "Tab" in line for line in xdotool_lines)

    # Also verify key() propagates xdotool failure instead of being masked by sleep.
    bin_dir2, trace2 = _install_fake_x11_bins(tmp_path / "keycase", wmctrl_raise_rc=0, xdotool_fail_on="key")
    monkeypatch.setenv("PATH", f"{bin_dir2}{os.pathsep}{os.environ.get('PATH', '')}")
    g2 = X11Gui(None, "u", display=":99", timeout=10)
    with pytest.raises(GuiActionError, match="keys to .* failed"):
        g2.key("Choosing Analyses", "Return", settle=0.01)
    assert any("xdotool key Return" in line for line in trace2.read_text(encoding="utf-8").splitlines())


def test_x11_gui_scratch_dir_expands_tilde_remote_and_local(monkeypatch, tmp_path):
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/vb_scratch")
    monkeypatch.setenv("VB_CLIENT_ID", "client1")

    class _TildeRunner:
        def run_command(self, command, timeout=None):
            if "getent" in command:
                return SimpleNamespace(returncode=0, stdout="/home/remote\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    remote_gui = X11Gui(_TildeRunner(), "u", display=":1")
    assert (
        remote_gui._scratch_dir()
        == "/home/remote/vb_scratch/virtuoso_bridge_u/client1/screenshots"
    )

    local_home = tmp_path / "local_home"
    monkeypatch.setenv("HOME", str(local_home))
    monkeypatch.setenv("USERPROFILE", str(local_home))
    local_gui = X11Gui(None, "u", display=":1")
    local_dir = local_gui._scratch_dir()
    assert local_dir == f"{str(local_home).rstrip('/')}/vb_scratch/virtuoso_bridge_u/client1/screenshots"
    assert not local_dir.startswith("~")

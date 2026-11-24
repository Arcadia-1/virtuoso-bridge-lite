import shlex
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

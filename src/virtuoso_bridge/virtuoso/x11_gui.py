"""Last-resort GUI driving of Virtuoso forms over X11 (xdotool/wmctrl/xwininfo/ImageMagick).

Some ADE/Maestro form state only reaches the netlist after the form itself has
been opened and confirmed: ``maeSetAnalysis`` accepts the option, stores it,
and the netlister ignores it. Known cases on IC23.1 are the Transient Noise
toggle of ``tran``, the jitter-event table of sampled ``pnoise``
(``measTableData``) and ``pss`` ``autotstab``. When no SKILL path exists, an
agent can open the form and click it like a person would.

This module is that fallback. It is deliberately small and strict:

* every action names one window by a title regex; zero or several matches
  refuse, and nothing ever acts on "whatever window is focused";
* click and type coordinates are relative to that window's client area, and a
  point outside the window refuses, so a stale coordinate cannot land in
  another application;
* :func:`screenshot` crops to the same client area, so the coordinates you read
  off a screenshot are the coordinates you pass back.

Coordinates still depend on the window layout, fonts and resolution, so do not
replay them blindly: pin the window geometry, screenshot, act, screenshot
again, and finally check the generated netlist (for Maestro:
``client.maestro.create_netlist_for_corner`` + ``check_netlist_options``),
never the form, to decide whether the setting took effect.

Commands run through ``bash -c`` so a csh login shell on the remote host does
not reinterpret quoting or redirections.
"""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from virtuoso_bridge.env import load_vb_env
from virtuoso_bridge.transport.remote_paths import default_virtuoso_bridge_dir, resolve_client_id
from virtuoso_bridge.virtuoso.x11 import _get_display, _run

REQUIRED_TOOLS = ("xdotool", "wmctrl", "xwininfo", "import", "convert")

# DISPLAY of the user's running Virtuoso, when VB_DISPLAY is not configured.
_DETECT_DISPLAY = (
    'for p in $(pgrep -u "$(id -un)" -f "dfII/bin/.*virtuoso|bin/64bit/virtuoso"); do '
    'tr "\\0" "\\n" < /proc/$p/environ 2>/dev/null | sed -n "s/^DISPLAY=//p"; '
    'done | head -1'
)


class GuiActionError(RuntimeError):
    """A GUI action was refused or failed; no further input was sent."""


@dataclass(frozen=True)
class GuiWindow:
    """One top-level X11 window, geometry of its client area in root coordinates."""

    window_id: str
    title: str
    x: int
    y: int
    w: int
    h: int

    def to_root(self, x: int, y: int) -> tuple[int, int]:
        """Translate window-relative (x, y) to root coordinates, refusing points outside."""
        if not (0 <= x < self.w and 0 <= y < self.h):
            raise GuiActionError(
                f"point ({x},{y}) lies outside window {self.title!r} "
                f"({self.w}x{self.h}); take a new screenshot and recompute")
        return self.x + x, self.y + y


class X11Gui:
    """Drive top-level windows on the display that shows Virtuoso.

    *runner* is an :class:`~virtuoso_bridge.transport.ssh.SSHRunner`, or
    ``None`` when the bridge runs on the GUI host itself (local mode).
    """

    def __init__(self, runner, user: str = "", *, display: str | None = None,
                 profile: str | None = None, timeout: int = 30) -> None:
        load_vb_env()
        self._runner = runner
        self._user = user
        self._profile = profile
        self._timeout = timeout
        self._display = _get_display(display) or self._detect_display()

    # -- plumbing -------------------------------------------------------------

    @property
    def display(self) -> str:
        return self._display

    def _sh(self, script: str, *, display: bool = True, timeout: int | None = None):
        if display:
            script = f"export DISPLAY={shlex.quote(self._display)}; {script}"
        return _run(self._runner, f"bash -c {shlex.quote(script)}", timeout or self._timeout)

    def _check(self, script: str, what: str) -> str:
        result = self._sh(script)
        if result.returncode != 0:
            raise GuiActionError(f"{what} failed (exit {result.returncode}): "
                                 f"{(result.stderr or result.stdout or '').strip()}")
        return result.stdout or ""

    def _detect_display(self) -> str:
        found = (self._sh(_DETECT_DISPLAY, display=False).stdout or "").strip()
        if not found:
            raise GuiActionError(
                "cannot tell which X display shows Virtuoso; set VB_DISPLAY (e.g. :1) "
                "or pass display=")
        return found.splitlines()[0]

    # -- discovery ------------------------------------------------------------

    def missing_tools(self) -> list[str]:
        """Tools from :data:`REQUIRED_TOOLS` that are not on the GUI host's PATH."""
        out = self._sh("for t in " + " ".join(REQUIRED_TOOLS)
                       + '; do command -v "$t" >/dev/null || echo "$t"; done',
                       display=False).stdout or ""
        return [t for t in out.split() if t]

    def windows(self, title: str | None = None) -> list[GuiWindow]:
        """Top-level windows whose title matches the regex *title* (all when None)."""
        out = self._check("wmctrl -l", "wmctrl -l")
        pattern = re.compile(title) if title else None
        found = []
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            wid, _, _, name = parts
            if pattern and not pattern.search(name):
                continue
            found.append(self._geometry(wid, name))
        return found

    def _geometry(self, window_id: str, title: str) -> GuiWindow:
        # xwininfo's absolute origin is the client area as drawn; xdotool and
        # wmctrl -lG are off by the title bar under GNOME/mutter.
        out = self._check(f"xwininfo -id {shlex.quote(window_id)}", f"geometry of {title!r}")
        keys = {"Absolute upper-left X": "x", "Absolute upper-left Y": "y",
                "Width": "w", "Height": "h"}
        geo = {}
        for line in out.splitlines():
            name, _, value = line.strip().partition(":")
            if name in keys:
                geo[keys[name]] = value.strip()
        try:
            return GuiWindow(window_id, title, int(geo["x"]), int(geo["y"]),
                             int(geo["w"]), int(geo["h"]))
        except (KeyError, ValueError) as exc:
            raise GuiActionError(f"unreadable geometry for {title!r}: {out!r}") from exc

    def find(self, title: str) -> GuiWindow:
        """Exactly one window whose title matches the regex *title*."""
        matches = self.windows(title)
        if len(matches) != 1:
            names = ", ".join(repr(w.title) for w in matches) or "none"
            raise GuiActionError(f"title /{title}/ must match exactly one window; matched {names}")
        return matches[0]

    # -- actions --------------------------------------------------------------

    def raise_window(self, window: GuiWindow) -> None:
        self._check(f"wmctrl -i -a {window.window_id}; sleep 0.5", f"raise {window.title!r}")

    def pin(self, title: str, x: int, y: int, w: int, h: int) -> GuiWindow:
        """Move and resize a window so that recorded coordinates stay valid."""
        window = self.find(title)
        self._check(f"wmctrl -i -r {window.window_id} -e 0,{x},{y},{w},{h}; sleep 0.5",
                    f"pin {window.title!r}")
        return self.find(title)

    def click(self, title: str, x: int, y: int, *, count: int = 1, button: int = 1,
              settle: float = 1.0) -> dict[str, Any]:
        """Click at window-relative (x, y); count=2 double-clicks."""
        window = self.find(title)
        rx, ry = window.to_root(x, y)
        self.raise_window(window)
        self._check(f"xdotool mousemove --sync {rx} {ry} click --repeat {int(count)} "
                    f"--delay 80 {int(button)}; sleep {settle}", f"click in {window.title!r}")
        return {"action": "click", "window": window.title, "at": [x, y], "root": [rx, ry],
                "count": count}

    def set_text(self, title: str, x: int, y: int, text: str, *, settle: float = 0.5) -> dict[str, Any]:
        """Replace the contents of the text field at (x, y) with *text*, then Tab out."""
        window = self.find(title)
        rx, ry = window.to_root(x, y)
        self.raise_window(window)
        self._check(f"xdotool mousemove --sync {rx} {ry} click --repeat 3 --delay 60 1; "
                    f"xdotool key ctrl+a; xdotool type --delay 30 -- {shlex.quote(text)}; "
                    f"xdotool key Tab; sleep {settle}", f"type into {window.title!r}")
        return {"action": "set_text", "window": window.title, "at": [x, y], "text": text}

    def key(self, title: str, *keys: str, settle: float = 0.5) -> dict[str, Any]:
        """Send key chords (xdotool names, e.g. ``ctrl+s``, ``Return``) to *title*."""
        if not keys:
            raise GuiActionError("key needs at least one key name")
        window = self.find(title)
        self.raise_window(window)
        self._check("xdotool key " + " ".join(shlex.quote(k) for k in keys) + f"; sleep {settle}",
                    f"keys to {window.title!r}")
        return {"action": "key", "window": window.title, "keys": list(keys)}

    def screenshot(self, output: str | Path, *, title: str | None = None,
                   region: tuple[int, int, int, int] | None = None,
                   scale: float | None = None) -> Path:
        """Save a PNG of the whole display, or of one window's client area.

        *region* (x, y, w, h) crops further, relative to the window (or root).
        Pixel (px, py) of an unscaled window shot is window coordinate
        (px + region x, py + region y).
        """
        ox = oy = 0
        crop = None
        if title is not None:
            window = self.find(title)
            self.raise_window(window)
            ox, oy = window.x, window.y
            crop = (window.w, window.h, window.x, window.y)
        if region is not None:
            rx, ry, rw, rh = (int(v) for v in region)
            crop = (rw, rh, ox + rx, oy + ry)
        remote_dir = self._scratch_dir()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        remote = f"{remote_dir}/gui_{stamp}.png"
        ops = []
        if crop:
            ops.append("-crop {}x{}+{}+{} +repage".format(*crop))
        if scale:
            ops.append(f"-resize {float(scale) * 100:g}%")
        q = shlex.quote(remote)
        self._check(f"mkdir -p {shlex.quote(remote_dir)} && import -window root {q}"
                    + (f" && convert {q} {' '.join(ops)} {q}" if ops else ""), "screenshot")
        output = Path(output)
        if output.is_dir():
            output = output / Path(remote).name
        output.parent.mkdir(parents=True, exist_ok=True)
        if self._runner is None:
            shutil.copyfile(remote, output)
        else:
            result = self._runner.download(remote, output)
            if getattr(result, "returncode", 0) != 0:
                raise GuiActionError(f"screenshot download failed: {getattr(result, 'stderr', '')}")
        self._sh(f"rm -f {q}", display=False)
        return output

    def _scratch_dir(self) -> str:
        if self._runner is None:
            return str(Path(default_virtuoso_bridge_dir(self._user, "screenshots",
                                                        resolve_client_id(self._profile))).expanduser())
        return default_virtuoso_bridge_dir(self._user, "screenshots", resolve_client_id(self._profile))


def window_dict(window: GuiWindow) -> dict[str, Any]:
    return asdict(window)

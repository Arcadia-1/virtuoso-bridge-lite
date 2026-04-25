# Dialog Dismissal Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically detect and close blocking Virtuoso GUI dialogs via X11, bypassing the stuck SKILL channel.

**Architecture:** When a SKILL `execute_skill()` call times out, it's usually because a modal dialog (e.g. "Save Changes", "Warning") has blocked the CIW event loop. Since the SKILL channel is dead, we use direct SSH to: (1) find the Virtuoso process's DISPLAY, (2) locate dialog windows via `xwininfo`, (3) send Enter/Escape keystrokes via Python2 + ctypes XTest on the remote machine. Exposed as both a Python API method on VirtuosoClient and a CLI command `virtuoso-bridge dismiss-dialog`.

**Tech Stack:** Python, SSH (via existing SSHRunner), remote Python2 + ctypes (Xlib/XTest), xwininfo

---

### Task 1: Remote X11 helper script

A Python2 script uploaded to the remote machine that does the actual X11 work: find dialog windows, take optional screenshot, send keystrokes. This is the only code that runs remotely; everything else is local orchestration.

**Files:**
- Create: `src/virtuoso_bridge/resources/x11_dismiss_dialog.py`

- [ ] **Step 1: Create the remote helper script**

```python
#!/usr/bin/env python2
"""X11 dialog finder and dismisser. Runs on the remote Virtuoso host.

Usage:
    python2 x11_dismiss_dialog.py <DISPLAY> [--dismiss] [--screenshot /tmp/out.ppm]

Output (stdout): JSON lines, one per dialog found:
    {"window_id": "0x2e01f16", "title": "Save Changes", "x": 1010, "y": 378, "w": 239, "h": 142}

With --dismiss: sends Enter key to each dialog found.
With --screenshot: saves a fullscreen PPM screenshot to the given path.

Exit codes: 0 = dialogs found/dismissed, 1 = no dialogs found, 2 = error
"""
import ctypes
import ctypes.util
import json
import os
import struct
import subprocess
import sys

DIALOG_TITLES = ["Save Changes", "Warning", "Error", "Confirm", "Question"]


def find_display(user=None):
    """Auto-detect DISPLAY from running virtuoso process if not given."""
    try:
        pids = subprocess.check_output(
            ["pgrep", "-u", user or os.environ["USER"], "-x", "virtuoso"],
            stderr=subprocess.PIPE
        ).strip().split("\n")
        for pid in pids:
            env_file = "/proc/%s/environ" % pid.strip()
            try:
                data = open(env_file, "rb").read()
                for chunk in data.split(b"\x00"):
                    if chunk.startswith(b"DISPLAY="):
                        return chunk.split(b"=", 1)[1].decode()
            except (IOError, OSError):
                continue
    except (subprocess.CalledProcessError, OSError):
        pass
    return None


def find_dialogs(display):
    """Use xwininfo to find dialog windows matching known titles."""
    os.environ["DISPLAY"] = display
    try:
        tree = subprocess.check_output(
            ["xwininfo", "-root", "-tree"],
            stderr=subprocess.PIPE
        ).decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, OSError) as e:
        print(json.dumps({"error": "xwininfo failed: %s" % str(e)}))
        return []

    dialogs = []
    for line in tree.splitlines():
        for title in DIALOG_TITLES:
            if ('"%s"' % title) in line:
                # Parse: 0x2e01f16 "Save Changes": ("virtuoso" "virtuoso")  239x142+1+38  +1010+378
                parts = line.strip().split()
                if len(parts) >= 1:
                    win_id = parts[0]
                    # Get geometry via xwininfo -id
                    try:
                        info = subprocess.check_output(
                            ["xwininfo", "-id", win_id],
                            stderr=subprocess.PIPE
                        ).decode("utf-8", errors="replace")
                        x = y = w = h = 0
                        for il in info.splitlines():
                            il = il.strip()
                            if il.startswith("Absolute upper-left X:"):
                                x = int(il.split(":")[1].strip())
                            elif il.startswith("Absolute upper-left Y:"):
                                y = int(il.split(":")[1].strip())
                            elif il.startswith("Width:"):
                                w = int(il.split(":")[1].strip())
                            elif il.startswith("Height:"):
                                h = int(il.split(":")[1].strip())
                        dialogs.append({
                            "window_id": win_id,
                            "title": title,
                            "x": x, "y": y, "w": w, "h": h,
                        })
                    except (subprocess.CalledProcessError, OSError):
                        dialogs.append({"window_id": win_id, "title": title})
                break
    return dialogs


def dismiss_window(display, win_id_str):
    """Send Enter key to a window via XTest."""
    os.environ["DISPLAY"] = display
    xlib_path = ctypes.util.find_library("X11")
    xtst_path = ctypes.util.find_library("Xtst")
    if not xlib_path or not xtst_path:
        return {"error": "libX11 or libXtst not found"}

    xlib = ctypes.cdll.LoadLibrary(xlib_path)
    xtst = ctypes.cdll.LoadLibrary(xtst_path)

    dpy = xlib.XOpenDisplay(None)
    if not dpy:
        return {"error": "cannot open display %s" % display}

    win_id = int(win_id_str, 16) if win_id_str.startswith("0x") else int(win_id_str)

    # Raise and focus the dialog
    xlib.XRaiseWindow(dpy, win_id)
    xlib.XSetInputFocus(dpy, win_id, 1, 0)  # RevertToParent
    xlib.XFlush(dpy)

    import time
    time.sleep(0.1)

    # Send Return key
    keysym_return = 0xff0d
    keycode = xlib.XKeysymToKeycode(dpy, keysym_return)
    xtst.XTestFakeKeyEvent(dpy, keycode, True, 0)
    xtst.XTestFakeKeyEvent(dpy, keycode, False, 0)
    xlib.XFlush(dpy)

    xlib.XCloseDisplay(dpy)
    return {"dismissed": win_id_str, "keycode": keycode}


def screenshot_ppm(display, output_path):
    """Take a fullscreen screenshot, save as PPM."""
    os.environ["DISPLAY"] = display
    try:
        subprocess.check_call(
            ["xwd", "-root", "-silent", "-out", "/tmp/_vb_screen.xwd"],
            stderr=subprocess.PIPE
        )
    except (subprocess.CalledProcessError, OSError) as e:
        return {"error": "xwd failed: %s" % str(e)}

    # Convert XWD to PPM
    data = open("/tmp/_vb_screen.xwd", "rb").read()
    hs = struct.unpack(">I", data[0:4])[0]
    w = struct.unpack(">I", data[16:20])[0]
    h = struct.unpack(">I", data[20:24])[0]
    bpp = struct.unpack(">I", data[44:48])[0]
    bpl = struct.unpack(">I", data[48:52])[0]
    pixels = data[hs:]

    rgb = bytearray()
    for y_row in range(h):
        row = pixels[y_row * bpl: y_row * bpl + w * (bpp // 8)]
        for x_col in range(w):
            if bpp == 32:
                b, g, r = ord(row[x_col*4]), ord(row[x_col*4+1]), ord(row[x_col*4+2])
            else:
                b, g, r = ord(row[x_col*3]), ord(row[x_col*3+1]), ord(row[x_col*3+2])
            rgb.append(r)
            rgb.append(g)
            rgb.append(b)

    with open(output_path, "wb") as f:
        f.write("P6\n%d %d\n255\n" % (w, h))
        f.write(bytes(rgb))

    try:
        os.remove("/tmp/_vb_screen.xwd")
    except OSError:
        pass
    return {"screenshot": output_path, "size": [w, h]}


def main():
    args = sys.argv[1:]
    display = None
    do_dismiss = False
    screenshot_path = None

    i = 0
    while i < len(args):
        if args[i] == "--dismiss":
            do_dismiss = True
        elif args[i] == "--screenshot":
            i += 1
            screenshot_path = args[i] if i < len(args) else "/tmp/_vb_screenshot.ppm"
        elif not args[i].startswith("-"):
            display = args[i]
        i += 1

    if not display:
        display = find_display()
        if not display:
            print(json.dumps({"error": "cannot detect DISPLAY"}))
            sys.exit(2)

    # Screenshot if requested
    if screenshot_path:
        result = screenshot_ppm(display, screenshot_path)
        print(json.dumps(result))

    # Find dialogs
    dialogs = find_dialogs(display)
    for d in dialogs:
        print(json.dumps(d))

    if not dialogs:
        sys.exit(1)

    # Dismiss if requested
    if do_dismiss:
        for d in dialogs:
            if "window_id" in d:
                result = dismiss_window(display, d["window_id"])
                print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add src/virtuoso_bridge/resources/x11_dismiss_dialog.py
git commit -m "feat: add remote X11 dialog finder/dismisser script"
```

---

### Task 2: Python API — `dismiss_dialog()` method on VirtuosoClient

Uploads the helper script via SSH, runs it, parses results. Exposed as `client.dismiss_dialog()`.

**Files:**
- Create: `src/virtuoso_bridge/virtuoso/x11.py`
- Modify: `src/virtuoso_bridge/virtuoso/basic/bridge.py`

- [ ] **Step 1: Create x11.py module**

```python
"""X11 dialog detection and dismissal via SSH (bypasses SKILL channel)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from virtuoso_bridge.transport.ssh import SSHRunner

logger = logging.getLogger(__name__)

_HELPER_SCRIPT = Path(__file__).parent.parent / "resources" / "x11_dismiss_dialog.py"
_REMOTE_SCRIPT = "/tmp/virtuoso_bridge_{user}/x11_dismiss_dialog.py"


def _ensure_helper(runner: SSHRunner, user: str) -> str:
    """Upload the helper script if not already present."""
    remote_path = _REMOTE_SCRIPT.format(user=user)
    remote_dir = str(Path(remote_path).parent)
    runner.run_command(f"mkdir -p {remote_dir}")
    runner.upload(_HELPER_SCRIPT, remote_path)
    return remote_path


def find_dialogs(runner: SSHRunner, user: str, display: str | None = None) -> list[dict[str, Any]]:
    """Find blocking dialog windows on the remote X11 display.

    Args:
        runner: SSHRunner connected to the remote host.
        user: Remote username (for process discovery).
        display: X11 DISPLAY string. If None, auto-detected from virtuoso process.

    Returns:
        List of dialog dicts: [{"window_id", "title", "x", "y", "w", "h"}, ...]
    """
    script = _ensure_helper(runner, user)
    cmd = f"python2 {script}"
    if display:
        cmd += f" {display}"
    result = runner.run_command(cmd, timeout=15)
    return _parse_output(result.stdout)


def dismiss_dialogs(
    runner: SSHRunner,
    user: str,
    display: str | None = None,
) -> list[dict[str, Any]]:
    """Find and dismiss all blocking dialog windows.

    Returns:
        List of result dicts (found dialogs + dismissal results).
    """
    script = _ensure_helper(runner, user)
    cmd = f"python2 {script} --dismiss"
    if display:
        cmd += f" {display}"
    result = runner.run_command(cmd, timeout=15)
    return _parse_output(result.stdout)


def screenshot(
    runner: SSHRunner,
    user: str,
    local_path: str | Path,
    display: str | None = None,
) -> dict[str, Any]:
    """Take a fullscreen X11 screenshot, download as PNG.

    Args:
        local_path: Local path to save the screenshot (will be PNG).
    """
    script = _ensure_helper(runner, user)
    remote_ppm = f"/tmp/virtuoso_bridge_{user}/x11_screenshot.ppm"
    cmd = f"python2 {script} --screenshot {remote_ppm}"
    if display:
        cmd += f" {display}"
    result = runner.run_command(cmd, timeout=30)
    parsed = _parse_output(result.stdout)

    # Download PPM and convert to PNG locally
    local_path = Path(local_path)
    local_ppm = local_path.with_suffix(".ppm")
    runner.download(remote_ppm, str(local_ppm))

    # Convert PPM to PNG
    try:
        from PIL import Image
        img = Image.open(str(local_ppm))
        img.save(str(local_path))
        local_ppm.unlink(missing_ok=True)
        info = {"local_path": str(local_path), "format": "png"}
    except ImportError:
        info = {"local_path": str(local_ppm), "format": "ppm", "note": "install Pillow for PNG"}

    return {**info, "remote_results": parsed}


def _parse_output(stdout: str) -> list[dict[str, Any]]:
    """Parse JSON-lines output from the helper script."""
    results = []
    for line in (stdout or "").strip().splitlines():
        line = line.strip()
        if line:
            try:
                results.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                logger.debug("Non-JSON line from helper: %s", line)
    return results
```

- [ ] **Step 2: Add `dismiss_dialog()` and `x11_screenshot()` to VirtuosoClient**

In `src/virtuoso_bridge/virtuoso/basic/bridge.py`, add methods:

```python
# Add import at top:
from virtuoso_bridge.virtuoso import x11

# Add methods to VirtuosoClient class:

def dismiss_dialog(self, display: str | None = None) -> list[dict]:
    """Find and dismiss blocking GUI dialogs via X11 (bypasses SKILL channel).

    Use when execute_skill() times out due to a modal dialog blocking CIW.
    Works via direct SSH + X11, independent of the SKILL channel.

    Returns:
        List of found/dismissed dialog info dicts.
    """
    runner = self.ssh_runner
    if runner is None:
        raise RuntimeError("No SSH connection available (tunnel not started?)")
    user = os.getenv("VB_REMOTE_USER", "")
    return x11.dismiss_dialogs(runner, user, display)

def x11_screenshot(self, local_path: str, display: str | None = None) -> dict:
    """Take a fullscreen X11 screenshot of the remote display.

    Works via direct SSH, independent of the SKILL channel.
    """
    runner = self.ssh_runner
    if runner is None:
        raise RuntimeError("No SSH connection available (tunnel not started?)")
    user = os.getenv("VB_REMOTE_USER", "")
    return x11.screenshot(runner, user, local_path, display)
```

- [ ] **Step 3: Commit**

```bash
git add src/virtuoso_bridge/virtuoso/x11.py src/virtuoso_bridge/virtuoso/basic/bridge.py
git commit -m "feat: add dismiss_dialog() and x11_screenshot() to VirtuosoClient"
```

---

### Task 3: CLI command — `virtuoso-bridge dismiss-dialog`

Add subcommand to the CLI so users can run it from terminal.

**Files:**
- Modify: `src/virtuoso_bridge/cli.py`

- [ ] **Step 1: Add CLI functions and register command**

In `cli.py`, add the handler function:

```python
def cli_dismiss_dialog(args: argparse.Namespace) -> int:
    """Find and dismiss blocking Virtuoso GUI dialogs."""
    from virtuoso_bridge.virtuoso import x11
    from virtuoso_bridge.transport.ssh import SSHRunner

    profile = _CLI_PROFILE[0] if _CLI_PROFILE else None
    suffix = f"_{profile}" if profile else ""
    remote_host = os.getenv(f"VB_REMOTE_HOST{suffix}", "").strip()
    remote_user = os.getenv(f"VB_REMOTE_USER{suffix}", "").strip()
    jump_host = os.getenv(f"VB_JUMP_HOST{suffix}", "").strip() or None
    jump_user = os.getenv(f"VB_JUMP_USER{suffix}", remote_user).strip() or None
    display = getattr(args, "display", None)

    if not remote_host:
        print("Error: VB_REMOTE_HOST not set")
        return 1

    runner = SSHRunner(
        remote_host=remote_host,
        remote_user=remote_user,
        jump_host=jump_host,
        jump_user=jump_user,
    )

    # Screenshot mode
    if getattr(args, "screenshot", None):
        print(f"[screenshot] Capturing remote display ...")
        result = x11.screenshot(runner, remote_user, args.screenshot, display)
        print(f"[screenshot] {result.get('local_path', 'unknown')}")
        return 0

    # Find/dismiss mode
    scan_only = getattr(args, "scan", False)
    if scan_only:
        print("[scan] Looking for dialog windows ...")
        dialogs = x11.find_dialogs(runner, remote_user, display)
    else:
        print("[dismiss] Looking for and dismissing dialog windows ...")
        dialogs = x11.dismiss_dialogs(runner, remote_user, display)

    if not dialogs:
        print("No dialog windows found.")
        return 1

    for d in dialogs:
        if "error" in d:
            print(f"  Error: {d['error']}")
        elif "dismissed" in d:
            print(f"  Dismissed: {d['dismissed']}")
        elif "title" in d:
            print(f"  Found: \"{d['title']}\" at ({d.get('x',0)},{d.get('y',0)}) {d.get('w',0)}x{d.get('h',0)}")

    return 0
```

In `build_parser()`, add the subparser:

```python
p_dismiss = sub.add_parser("dismiss-dialog", help="Find and dismiss blocking Virtuoso GUI dialogs")
p_dismiss.add_argument("--scan", action="store_true", help="Only scan for dialogs, don't dismiss")
p_dismiss.add_argument("--display", help="X11 DISPLAY (auto-detected if omitted)")
p_dismiss.add_argument("--screenshot", metavar="PATH", help="Save fullscreen screenshot instead")
```

In the dispatch dict, add:

```python
"dismiss-dialog": cli_dismiss_dialog,
```

- [ ] **Step 2: Commit**

```bash
git add src/virtuoso_bridge/cli.py
git commit -m "feat: add 'virtuoso-bridge dismiss-dialog' CLI command"
```

---

### Task 4: Integration test — end-to-end verify

**Files:**
- Create: `examples/01_virtuoso/basic/07_dismiss_dialog.py`

- [ ] **Step 1: Create example script**

```python
#!/usr/bin/env python3
"""Demonstrate X11 dialog detection and dismissal.

Usage::

    python 07_dismiss_dialog.py              # scan for dialogs
    python 07_dismiss_dialog.py --dismiss    # scan and dismiss
    python 07_dismiss_dialog.py --screenshot # take fullscreen screenshot

Prerequisites:
  - virtuoso-bridge service running (virtuoso-bridge start)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from virtuoso_bridge import VirtuosoClient


def main() -> int:
    client = VirtuosoClient.from_env()

    if "--screenshot" in sys.argv:
        out = Path("output/x11_screenshot.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        result = client.x11_screenshot(str(out))
        print(f"Screenshot: {result}")
        return 0

    if "--dismiss" in sys.argv:
        results = client.dismiss_dialog()
        print(f"Dismiss results: {results}")
    else:
        # Just check — import x11 directly for scan-only
        from virtuoso_bridge.virtuoso import x11
        runner = client.ssh_runner
        user = client._tunnel._ssh_runner._remote_user
        dialogs = x11.find_dialogs(runner, user)
        print(f"Dialogs found: {dialogs}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Test scan mode (no dialog expected)**

```bash
python examples/01_virtuoso/basic/07_dismiss_dialog.py
# Expected: "Dialogs found: []"
```

- [ ] **Step 3: Test screenshot**

```bash
python examples/01_virtuoso/basic/07_dismiss_dialog.py --screenshot
# Expected: "Screenshot: {'local_path': 'output/x11_screenshot.png', ...}"
```

- [ ] **Step 4: Commit**

```bash
git add examples/01_virtuoso/basic/07_dismiss_dialog.py
git commit -m "feat: add dialog dismiss example script"
```

---

### Task 5: Update skill docs

**Files:**
- Modify: `skills/virtuoso/references/troubleshooting.md`
- Modify: `skills/virtuoso/SKILL.md`

- [ ] **Step 1: Add dismiss-dialog to troubleshooting docs**

Add section to troubleshooting.md about dialog blocking recovery:

```markdown
### GUI Dialog Blocking — Recovery

When `execute_skill()` times out and the SKILL channel is unresponsive,
a modal dialog is likely blocking the CIW event loop.

**Python API recovery:**
```python
# Dismiss all blocking dialogs via X11 (bypasses SKILL)
results = client.dismiss_dialog()

# Or take a screenshot first to see what's happening
client.x11_screenshot("debug_screen.png")
```

**CLI recovery:**
```bash
virtuoso-bridge dismiss-dialog              # find and dismiss
virtuoso-bridge dismiss-dialog --scan       # scan only
virtuoso-bridge dismiss-dialog --screenshot output.png  # screenshot
```

**Prevention:** Always `dbSave(cv)` before `hiCloseWindow(win)` to avoid
"Save Changes" dialogs.
```

- [ ] **Step 2: Add brief mention in SKILL.md Gotchas section**

- [ ] **Step 3: Commit**

```bash
git add skills/virtuoso/references/troubleshooting.md skills/virtuoso/SKILL.md
git commit -m "docs: add dialog dismissal to troubleshooting"
```

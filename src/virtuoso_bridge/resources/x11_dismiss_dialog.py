#!/usr/bin/env python2
"""X11 dialog finder and dismisser. Runs on the remote Virtuoso host.

Usage:
    python2 x11_dismiss_dialog.py [DISPLAY] [--dismiss]

Output (stdout): JSON lines, one per dialog found:
    {"window_id": "0x2e01f16", "title": "Save Changes", "x": 1010, "y": 378, "w": 239, "h": 142}

With --dismiss: sends Enter key to each dialog found.
DISPLAY auto-detected from running virtuoso process if omitted.

Exit codes: 0 = dialogs found/dismissed, 1 = no dialogs found, 2 = error
"""
import ctypes
import ctypes.util
import json
import os
import subprocess
import sys
import time

VIRTUOSO_WM_CLASSES = ["virtuoso", "libManager"]


def find_x11_env(user=None):
    """Auto-detect DISPLAY and XAUTHORITY from running virtuoso process.

    Skips batch virtuoso processes (those with -nograph in cmdline).
    If multiple candidates, prefers the interactive one.
    """
    candidates = []
    try:
        pids = subprocess.check_output(
            ["pgrep", "-u", user or os.environ.get("USER", ""), "-x", "virtuoso"],
            stderr=subprocess.PIPE
        ).strip().splitlines()
        for pid in pids:
            pid = pid.strip()
            if not pid:
                continue
            # Skip batch processes (have -nograph in cmdline)
            try:
                cmdline = open("/proc/%s/cmdline" % pid, "rb").read()
                if b"-nograph" in cmdline:
                    continue
            except (IOError, OSError):
                pass
            env_file = "/proc/%s/environ" % pid
            try:
                data = open(env_file, "rb").read()
                info = {}
                info["DISPLAY"] = None
                info["XAUTHORITY"] = None
                for chunk in data.split(b"\x00"):
                    if chunk.startswith(b"DISPLAY="):
                        info["DISPLAY"] = chunk.split(b"=", 1)[1].decode()
                    elif chunk.startswith(b"XAUTHORITY="):
                        info["XAUTHORITY"] = chunk.split(b"=", 1)[1].decode()
                if info["DISPLAY"]:
                    candidates.append(info)
            except (IOError, OSError):
                continue
    except (subprocess.CalledProcessError, OSError):
        pass

    if not candidates:
        return {"DISPLAY": None, "XAUTHORITY": None}

    # Prefer interactive display (not Xvfb-style small displays)
    # Heuristic: Xvfb displays often use high display numbers (:99, :1024)
    # Real user sessions use lower numbers or localhost:NN
    return candidates[0]


def find_dialogs(display):
    """Find top-level dialog windows belonging to Virtuoso.

    Only matches direct children of root window frames that contain a
    virtuoso-class window. These are the actual dialog popups, not
    toolbar/menu sub-widgets inside main windows.
    """
    os.environ["DISPLAY"] = display
    try:
        tree = subprocess.check_output(
            ["xwininfo", "-root", "-children"],
            stderr=subprocess.PIPE
        ).decode("utf-8", "replace")
    except (subprocess.CalledProcessError, OSError) as e:
        print(json.dumps({"error": "xwininfo failed: %s" % str(e)}))
        return []

    # -children gives ONLY direct children of root (top-level frames).
    # Each top-level frame wraps one app window. We need to check if the
    # frame contains a virtuoso-class child that looks like a dialog.
    # Use -tree on each candidate to inspect its children.

    # Step 1: collect top-level frame IDs with small size (dialog candidates)
    candidates = []
    in_children = False
    for line in tree.splitlines():
        if "children" in line.lower() and ":" in line:
            in_children = True
            continue
        if not in_children:
            continue
        parts = line.strip().split()
        if not parts or not parts[0].startswith("0x"):
            continue
        win_id = parts[0]

        # Parse inline geometry: NNNxMMM+X+Y
        # e.g. "241x181+1009+340"
        geo_w = geo_h = 0
        for p in parts:
            if "x" in p and "+" in p and p[0].isdigit():
                try:
                    size, _, pos = p.partition("+")
                    geo_w, geo_h = [int(v) for v in size.split("x")]
                except (ValueError, IndexError):
                    pass

        # Skip very tiny windows
        if geo_w < 20 or geo_h < 20:
            continue
        # Thresholds derived from observed Virtuoso window sizes at 1x DPI:
        #   Modal dialogs:  ~300-600w × 100-350h  (e.g. ADE "Update and Run" ≈ 580×140)
        #   Editor/log panes: ~500-800w × 500-900h (e.g. spectre.out ≈ 535×755)
        #   Main app frames: 1200+w × 700+h
        # Skip tall windows (editor/result panes, not modal dialogs).
        if geo_h > 420:
            continue
        # Skip windows that are very large in both dimensions (main app frames).
        if geo_w > 1000 and geo_h > 300:
            continue

        candidates.append(win_id)

    # Step 2: for each candidate, check if it contains a virtuoso-class child
    dialogs = []
    for win_id in candidates:
        try:
            subtree = subprocess.check_output(
                ["xwininfo", "-id", win_id, "-tree"],
                stderr=subprocess.PIPE
            ).decode("utf-8", "replace")
        except (subprocess.CalledProcessError, OSError):
            continue

        is_virtuoso = False
        child_title = ""
        for sl in subtree.splitlines():
            for cls in VIRTUOSO_WM_CLASSES:
                if ('"%s"' % cls) in sl:
                    is_virtuoso = True
                    # Extract title
                    if '"' in sl:
                        start = sl.index('"') + 1
                        end = sl.index('"', start)
                        child_title = sl[start:end]
                    break
            if is_virtuoso:
                break

        if not is_virtuoso:
            continue

        # Get precise geometry
        try:
            info = subprocess.check_output(
                ["xwininfo", "-id", win_id],
                stderr=subprocess.PIPE
            ).decode("utf-8", "replace")
            x = y = w = h = 0
            mapped = False
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
                elif "Map State:" in il and "IsViewable" in il:
                    mapped = True
            if not mapped:
                continue
        except (subprocess.CalledProcessError, OSError):
            continue

        dialogs.append({
            "window_id": win_id,
            "title": child_title,
            "x": x, "y": y, "w": w, "h": h,
        })

    return dialogs


def _find_app_child(display, frame_id_str):
    """Find the actual app window inside a WM frame (first named child)."""
    try:
        tree = subprocess.check_output(
            ["xwininfo", "-id", frame_id_str, "-children"],
            stderr=subprocess.PIPE
        ).decode("utf-8", "replace")
        for line in tree.splitlines():
            line = line.strip()
            if line.startswith("0x") and '"' in line:
                return line.split()[0]
    except (subprocess.CalledProcessError, OSError):
        pass
    return frame_id_str  # fallback to frame itself


def _send_alt_n(dpy, xlib, xtst):
    """Send Alt+N to trigger the No button mnemonic."""
    keysym_alt_l = 0xffe9  # XK_Alt_L
    keysym_n = 0x006e      # XK_n
    kc_alt = xlib.XKeysymToKeycode(dpy, keysym_alt_l)
    kc_n = xlib.XKeysymToKeycode(dpy, keysym_n)

    xtst.XTestFakeKeyEvent(dpy, kc_alt, True, 0)
    xtst.XTestFakeKeyEvent(dpy, kc_n, True, 0)
    xtst.XTestFakeKeyEvent(dpy, kc_n, False, 0)
    xtst.XTestFakeKeyEvent(dpy, kc_alt, False, 0)
    xlib.XFlush(dpy)
    return kc_alt, kc_n


def _send_alt_y(dpy, xlib, xtst):
    """Send Alt+Y to trigger the Yes button mnemonic."""
    keysym_alt_l = 0xffe9  # XK_Alt_L
    keysym_y = 0x0079      # XK_y
    kc_alt = xlib.XKeysymToKeycode(dpy, keysym_alt_l)
    kc_y = xlib.XKeysymToKeycode(dpy, keysym_y)

    xtst.XTestFakeKeyEvent(dpy, kc_alt, True, 0)
    xtst.XTestFakeKeyEvent(dpy, kc_y, True, 0)
    xtst.XTestFakeKeyEvent(dpy, kc_y, False, 0)
    xtst.XTestFakeKeyEvent(dpy, kc_alt, False, 0)
    xlib.XFlush(dpy)
    return kc_alt, kc_y


def _send_escape(dpy, xlib, xtst):
    """Send Escape key (maps to Cancel on most dialogs)."""
    keysym_esc = 0xff1b  # XK_Escape
    kc_esc = xlib.XKeysymToKeycode(dpy, keysym_esc)
    xtst.XTestFakeKeyEvent(dpy, kc_esc, True, 0)
    xtst.XTestFakeKeyEvent(dpy, kc_esc, False, 0)
    xlib.XFlush(dpy)
    return kc_esc


def dismiss_window(display, win_id_str, title="", x=0, y=0, w=0, h=0):
    """Dismiss a window via XTest.

    Default behavior is Enter.
    For Save As prompts, prefer 'n' (No) to avoid Save/Copy dialog loops.
    """
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

    # Focus the actual app child window, not the WM frame
    child_id_str = _find_app_child(display, win_id_str)
    child_id = int(child_id_str, 16) if child_id_str.startswith("0x") else int(child_id_str)

    xlib.XRaiseWindow(dpy, child_id)
    xlib.XSetInputFocus(dpy, child_id, 1, 0)  # RevertToParent
    xlib.XFlush(dpy)

    time.sleep(0.15)

    title_l = (title or "").lower()
    # Policy values:
    # - smart   : choose action by explicit context (dedupe -> No, default -> Cancel)
    # - discard : always choose No
    # - save    : always choose Yes
    # - cancel  : always choose Cancel
    save_policy = (os.environ.get("VB_SAVE_DIALOG_POLICY", "smart") or "smart").lower()
    save_context = (os.environ.get("VB_SAVE_DIALOG_CONTEXT", "") or "").lower()
    if ("save as" in title_l) or ("save a copy" in title_l):
        try:
            if save_policy == "discard":
                kc_alt, kc_n = _send_alt_n(dpy, xlib, xtst)
                xlib.XCloseDisplay(dpy)
                return {
                    "dismissed": win_id_str,
                    "child": child_id_str,
                    "action": "alt_n_no",
                    "title": title,
                    "policy": save_policy,
                    "keycode_alt": int(kc_alt),
                    "keycode_n": int(kc_n),
                }
            elif save_policy == "save":
                kc_alt, kc_y = _send_alt_y(dpy, xlib, xtst)
                xlib.XCloseDisplay(dpy)
                return {
                    "dismissed": win_id_str,
                    "child": child_id_str,
                    "action": "alt_y_yes",
                    "title": title,
                    "policy": save_policy,
                    "keycode_alt": int(kc_alt),
                    "keycode_y": int(kc_y),
                }
            elif save_policy == "cancel":
                kc_esc = _send_escape(dpy, xlib, xtst)
                xlib.XCloseDisplay(dpy)
                return {
                    "dismissed": win_id_str,
                    "child": child_id_str,
                    "action": "esc_cancel",
                    "title": title,
                    "policy": save_policy,
                    "keycode_esc": int(kc_esc),
                }
            else:
                if save_context == "dedupe":
                    kc_alt, kc_n = _send_alt_n(dpy, xlib, xtst)
                    xlib.XCloseDisplay(dpy)
                    return {
                        "dismissed": win_id_str,
                        "child": child_id_str,
                        "action": "alt_n_no_dedupe",
                        "title": title,
                        "policy": "smart",
                        "context": save_context,
                        "keycode_alt": int(kc_alt),
                        "keycode_n": int(kc_n),
                    }

                kc_esc = _send_escape(dpy, xlib, xtst)
                xlib.XCloseDisplay(dpy)
                return {
                    "dismissed": win_id_str,
                    "child": child_id_str,
                    "action": "esc_cancel_smart",
                    "title": title,
                    "policy": "smart",
                    "context": save_context,
                    "keycode_esc": int(kc_esc),
                }
        except Exception:
            # Fallback: send bare 'n' key and return immediately.
            keysym_n = 0x006e  # XK_n
            kc_n = xlib.XKeysymToKeycode(dpy, keysym_n)
            xtst.XTestFakeKeyEvent(dpy, kc_n, True, 0)
            xtst.XTestFakeKeyEvent(dpy, kc_n, False, 0)
            xlib.XFlush(dpy)
            xlib.XCloseDisplay(dpy)
            return {
                "dismissed": win_id_str,
                "child": child_id_str,
                "keycode": int(kc_n),
                "action": "no_fallback",
                "title": title,
            }
    else:
        keysym = 0xff0d  # XK_Return
        action = "enter"

    keycode = xlib.XKeysymToKeycode(dpy, keysym)
    xtst.XTestFakeKeyEvent(dpy, keycode, True, 0)
    xtst.XTestFakeKeyEvent(dpy, keycode, False, 0)
    xlib.XFlush(dpy)

    xlib.XCloseDisplay(dpy)
    return {
        "dismissed": win_id_str,
        "child": child_id_str,
        "keycode": int(keycode),
        "action": action,
        "title": title,
    }


def main():
    args = sys.argv[1:]
    display = None
    do_dismiss = False

    i = 0
    while i < len(args):
        if args[i] == "--dismiss":
            do_dismiss = True
        elif not args[i].startswith("-"):
            display = args[i]
        i += 1

    if not display:
        x11_env = find_x11_env()
        display = x11_env.get("DISPLAY")
        if not display:
            print(json.dumps({"error": "cannot detect DISPLAY"}))
            sys.exit(2)
        xauth = x11_env.get("XAUTHORITY")
        if isinstance(xauth, str) and xauth:
            os.environ["XAUTHORITY"] = xauth
    dialogs = find_dialogs(display)
    for d in dialogs:
        print(json.dumps(d))

    if not dialogs:
        sys.exit(1)

    if do_dismiss:
        for d in dialogs:
            if "window_id" in d:
                result = dismiss_window(
                    display,
                    d["window_id"],
                    d.get("title", ""),
                    d.get("x", 0),
                    d.get("y", 0),
                    d.get("w", 0),
                    d.get("h", 0),
                )
                print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()

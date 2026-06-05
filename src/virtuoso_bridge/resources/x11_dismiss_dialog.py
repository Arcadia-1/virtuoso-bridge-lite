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
import re
import subprocess
import sys
import time

try:
    string_types = (basestring,)
except NameError:
    string_types = (str,)

VIRTUOSO_WM_CLASSES = ["virtuoso", "libManager"]
KNOWN_MODAL_ACTIONS = {
    "ade explorer update and run": "enter",
}


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


def _parse_window_line(line):
    """Parse one xwininfo tree/children line."""
    line = line.strip()
    if not line.startswith("0x"):
        return None
    parts = line.split(None, 1)
    if not parts:
        return None
    win = {"id": parts[0], "title": "", "class": [], "geometry": {}}
    if '"' in line:
        try:
            start = line.index('"') + 1
            end = line.index('"', start)
            win["title"] = line[start:end]
        except ValueError:
            pass
    class_match = re.search(r":\s*\(([^)]*)\)", line)
    if class_match:
        win["class"] = re.findall(r'"([^"]*)"', class_match.group(1))
    geo_match = re.search(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", line)
    if geo_match:
        win["geometry"] = {
            "w": int(geo_match.group(1)),
            "h": int(geo_match.group(2)),
            "x": int(geo_match.group(3)),
            "y": int(geo_match.group(4)),
        }
    return win


def _is_virtuoso_class(classes):
    lowered = [c.lower() for c in (classes or [])]
    for cls in VIRTUOSO_WM_CLASSES:
        if cls.lower() in lowered:
            return True
    return False


def _read_window_info(win_id):
    try:
        info = subprocess.check_output(
            ["xwininfo", "-id", win_id],
            stderr=subprocess.PIPE
        ).decode("utf-8", "replace")
    except (subprocess.CalledProcessError, OSError):
        return {"geometry": {}, "mapped": False}
    geometry = {}
    mapped = False
    for il in info.splitlines():
        il = il.strip()
        try:
            if il.startswith("Absolute upper-left X:"):
                geometry["x"] = int(il.split(":")[1].strip())
            elif il.startswith("Absolute upper-left Y:"):
                geometry["y"] = int(il.split(":")[1].strip())
            elif il.startswith("Width:"):
                geometry["w"] = int(il.split(":")[1].strip())
            elif il.startswith("Height:"):
                geometry["h"] = int(il.split(":")[1].strip())
            elif "Map State:" in il and "IsViewable" in il:
                mapped = True
        except (ValueError, IndexError):
            pass
    return {"geometry": geometry, "mapped": mapped}


def _root_frames():
    try:
        tree = subprocess.check_output(
            ["xwininfo", "-root", "-children"],
            stderr=subprocess.PIPE
        ).decode("utf-8", "replace")
    except (subprocess.CalledProcessError, OSError) as e:
        print(json.dumps({"error": "xwininfo failed: %s" % str(e)}))
        return []
    frames = []
    in_children = False
    for line in tree.splitlines():
        if "children" in line.lower() and ":" in line:
            in_children = True
            continue
        if not in_children:
            continue
        frame = _parse_window_line(line)
        if not frame:
            continue
        info = _read_window_info(frame["id"])
        frame["geometry"] = info.get("geometry") or frame.get("geometry") or {}
        frame["mapped"] = info.get("mapped", False)
        frames.append(frame)
    return frames


def _frame_children(frame_id):
    try:
        subtree = subprocess.check_output(
            ["xwininfo", "-id", frame_id, "-tree"],
            stderr=subprocess.PIPE
        ).decode("utf-8", "replace")
    except (subprocess.CalledProcessError, OSError):
        return []
    children = []
    for line in subtree.splitlines():
        child = _parse_window_line(line)
        if child:
            children.append(child)
    return children


def _geometry_is_dialog_sized(geometry):
    geo_w = int(geometry.get("w") or 0)
    geo_h = int(geometry.get("h") or 0)
    if geo_w < 20 or geo_h < 20:
        return False
    if geo_h > 420:
        return False
    if geo_w > 1000 and geo_h > 300:
        return False
    return True


def _known_action(title):
    title_l = (title or "").lower()
    for needle, action in KNOWN_MODAL_ACTIONS.items():
        if needle in title_l:
            return action
    if ("save as" in title_l) or ("save a copy" in title_l):
        return "escape"
    return None


def classify_windows(windows):
    classified = []
    for win in windows:
        item = dict(win)
        action = _known_action(item.get("title") or "")
        if action:
            item["kind"] = "known_modal"
            item["suggested_action"] = action
        elif _geometry_is_dialog_sized(item.get("geometry") or {}):
            item["kind"] = "dialog_candidate"
            item["suggested_action"] = "enter"
        else:
            item["kind"] = "main_window"
            item["suggested_action"] = None
        classified.append(item)
    return classified


def discover_windows(display):
    """Enumerate Virtuoso-related X11 windows with frame and child details."""
    os.environ["DISPLAY"] = display
    windows = []
    seen = set()
    for frame in _root_frames():
        if not frame.get("mapped", False):
            continue
        frame_id = frame["id"]
        geometry = frame.get("geometry") or {}
        children = _frame_children(frame_id)
        app_children = [c for c in children if _is_virtuoso_class(c.get("class"))]
        if _is_virtuoso_class(frame.get("class")):
            app_children.append(frame)
        for child in app_children:
            dismiss_id = child["id"]
            key = (frame_id, dismiss_id)
            if key in seen:
                continue
            seen.add(key)
            windows.append({
                "frame_id": frame_id,
                "window_id": dismiss_id,
                "dismiss_id": dismiss_id,
                "title": child.get("title") or frame.get("title") or "",
                "class": child.get("class") or frame.get("class") or [],
                "geometry": {
                    "w": int(geometry.get("w") or 0),
                    "h": int(geometry.get("h") or 0),
                    "x": int(geometry.get("x") or 0),
                    "y": int(geometry.get("y") or 0),
                },
                "mapped": True,
            })
    return classify_windows(windows)


def _auto_dismissable(win):
    return win.get("kind") in ("known_modal", "dialog_candidate")


def find_dialogs(display):
    """Backward-compatible auto-dismiss candidate view of discover_windows()."""
    dialogs = []
    for win in discover_windows(display):
        if not _auto_dismissable(win):
            continue
        geo = win.get("geometry") or {}
        dialogs.append({
            "window_id": win.get("dismiss_id") or win.get("window_id"),
            "frame_id": win.get("frame_id"),
            "title": win.get("title", ""),
            "x": geo.get("x", 0),
            "y": geo.get("y", 0),
            "w": geo.get("w", 0),
            "h": geo.get("h", 0),
            "kind": win.get("kind"),
            "suggested_action": win.get("suggested_action"),
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


def _send_enter(dpy, xlib, xtst):
    """Send Return key."""
    keysym = 0xff0d  # XK_Return
    keycode = xlib.XKeysymToKeycode(dpy, keysym)
    xtst.XTestFakeKeyEvent(dpy, keycode, True, 0)
    xtst.XTestFakeKeyEvent(dpy, keycode, False, 0)
    xlib.XFlush(dpy)
    return keycode


def _send_explicit_action(dpy, xlib, xtst, action):
    normalized = (action or "enter").lower().replace("_", "-")
    if normalized == "enter":
        return "enter", {"keycode": int(_send_enter(dpy, xlib, xtst))}
    if normalized in ("escape", "esc"):
        return "escape", {"keycode_esc": int(_send_escape(dpy, xlib, xtst))}
    if normalized in ("alt-y", "yes"):
        kc_alt, kc_y = _send_alt_y(dpy, xlib, xtst)
        return "alt-y", {"keycode_alt": int(kc_alt), "keycode_y": int(kc_y)}
    if normalized in ("alt-n", "no"):
        kc_alt, kc_n = _send_alt_n(dpy, xlib, xtst)
        return "alt-n", {"keycode_alt": int(kc_alt), "keycode_n": int(kc_n)}
    raise ValueError("unsupported action: %s" % action)


def dismiss_window(display, win_id_str, title="", x=0, y=0, w=0, h=0, action=None):
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

    # Legacy auto mode accepts a WM frame and resolves to the app child.
    # Explicit dismiss-window mode focuses the exact caller-provided target.
    child_id_str = win_id_str if action else _find_app_child(display, win_id_str)
    child_id = int(child_id_str, 16) if child_id_str.startswith("0x") else int(child_id_str)

    xlib.XRaiseWindow(dpy, child_id)
    xlib.XSetInputFocus(dpy, child_id, 1, 0)  # RevertToParent
    xlib.XFlush(dpy)

    time.sleep(0.15)

    if action:
        try:
            action_name, extra = _send_explicit_action(dpy, xlib, xtst, action)
        except ValueError as exc:
            xlib.XCloseDisplay(dpy)
            return {"error": str(exc), "dismissed": win_id_str, "child": child_id_str}
        xlib.XCloseDisplay(dpy)
        result = {
            "dismissed": win_id_str,
            "child": child_id_str,
            "action": action_name,
            "title": title,
        }
        result.update(extra)
        return result

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
        keycode = _send_enter(dpy, xlib, xtst)
        action = "enter"

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
    list_windows = False
    dismiss_target = None
    action = "enter"

    i = 0
    while i < len(args):
        if args[i] == "--dismiss":
            do_dismiss = True
        elif args[i] == "--list-windows":
            list_windows = True
        elif args[i] == "--dismiss-window":
            if i + 1 >= len(args):
                print(json.dumps({"error": "--dismiss-window requires a window id"}))
                sys.exit(2)
            dismiss_target = args[i + 1]
            i += 1
        elif args[i] == "--action":
            if i + 1 >= len(args):
                print(json.dumps({"error": "--action requires a value"}))
                sys.exit(2)
            action = args[i + 1]
            i += 1
        elif args[i] == "--json":
            pass
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
        if isinstance(xauth, string_types) and xauth:
            os.environ["XAUTHORITY"] = xauth

    if dismiss_target:
        result = dismiss_window(display, dismiss_target, action=action)
        print(json.dumps(result))
        sys.exit(1 if "error" in result else 0)

    if list_windows:
        windows = discover_windows(display)
        for w in windows:
            print(json.dumps(w))
        sys.exit(0 if windows else 1)

    dialogs = find_dialogs(display)
    for d in dialogs:
        print(json.dumps(d))

    if not dialogs:
        sys.exit(1)

    if do_dismiss:
        for d in dialogs:
            if "window_id" in d:
                explicit_action = None
                if "ade explorer update and run" in (d.get("title", "") or "").lower():
                    explicit_action = d.get("suggested_action") or "enter"
                result = dismiss_window(
                    display,
                    d["window_id"],
                    d.get("title", ""),
                    d.get("x", 0),
                    d.get("y", 0),
                    d.get("w", 0),
                    d.get("h", 0),
                    explicit_action,
                )
                print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()

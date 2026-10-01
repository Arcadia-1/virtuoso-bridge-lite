#!/usr/bin/env python
"""Prepare and request one guarded X11 WM_DELETE_WINDOW operation.

The visual hash is only a stale-state guard.  It does not establish semantic
safety; approval of the dialog action remains the caller's responsibility.
"""

from __future__ import print_function

import base64
import ctypes
import ctypes.util
import hashlib
import json
import math
import os
import re
import signal
import struct
import sys
import zlib

try:
    from virtuoso_bridge.resources import x11_dismiss_dialog as _inventory
except ImportError:
    import x11_dismiss_dialog as _inventory


try:
    _TEXT_TYPES = (basestring,)
    _INTEGER_TYPES = (int, long)
except NameError:
    _TEXT_TYPES = (str,)
    _INTEGER_TYPES = (int,)


_MAX_STDIN_BYTES = 64 * 1024
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_TITLE_CHARS = 512
_MAX_DISPLAY_CHARS = 256
_MAX_TIMEOUT_SECONDS = 60.0
_DISPLAY_RE = re.compile(r"^[A-Za-z0-9_./\[\]:-]+(?:\.[0-9]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_IS_VIEWABLE = 2
_ZPIXMAP = 2
_LSB_FIRST = 0
_CLIENT_MESSAGE = 33
_CURRENT_TIME = 0
_NO_EVENT_MASK = 0
_XA_ATOM = 4
_XA_CARDINAL = 6
_XA_WINDOW = 33


class _Refused(Exception):
    pass


class _PossibleTransmission(Exception):
    pass


class _GrabExitTimer(object):
    """Kill only this helper if a native call stalls while X is grabbed."""

    def __init__(self, seconds):
        self._seconds = min(1.0, float(seconds))
        self._previous = None
        self._armed = False

    def arm(self):
        if not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM"):
            raise _Refused("a helper-local X grab timer is unavailable")
        self._previous = signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, self._seconds)
        self._armed = True

    def cancel(self):
        if not self._armed:
            return
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, self._previous)
        self._armed = False


def _grab_exit_timer(seconds):
    return _GrabExitTimer(seconds)


class _XClassHint(ctypes.Structure):
    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
        ("funcs", ctypes.c_void_p * 6),
    ]


class _ClientData(ctypes.Union):
    _fields_ = [
        ("b", ctypes.c_char * 20),
        ("s", ctypes.c_short * 10),
        ("l", ctypes.c_long * 5),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _ClientData),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("client", _XClientMessageEvent), ("pad", ctypes.c_long * 24)]


def _has_control(value):
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _require_text(value, name, maximum, allow_empty=False):
    if not isinstance(value, _TEXT_TYPES):
        raise _Refused("%s must be text" % name)
    if (not value and not allow_empty) or len(value) > maximum or _has_control(value):
        raise _Refused("%s is outside its accepted bounds" % name)
    return value


def _canonical_xid(value, name):
    if isinstance(value, bool) or not isinstance(value, (_TEXT_TYPES,) + _INTEGER_TYPES):
        raise _Refused("%s must be an X11 window id" % name)
    try:
        normalized = _inventory._normalize_xid(value)
        numeric = int(normalized, 16)
    except (TypeError, ValueError):
        raise _Refused("%s must be an X11 window id" % name)
    if numeric <= 0 or numeric > 0xffffffffffffffff:
        raise _Refused("%s is outside its accepted bounds" % name)
    return normalized


def _validate_command(value):
    if not isinstance(value, dict):
        raise _Refused("command must be a JSON object")
    operation = value.get("op")
    if operation not in ("prepare", "close"):
        raise _Refused("op must be prepare or close")
    if operation == "prepare":
        expected = set(("op", "pid", "display", "ciw_window", "window_id", "title", "timeout"))
    else:
        expected = set(("op", "snapshot", "timeout"))
    if set(value) != expected:
        raise _Refused("command fields do not match the operation schema")
    timeout = value["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise _Refused("timeout must be numeric")
    timeout = float(timeout)
    if math.isinf(timeout) or math.isnan(timeout):
        raise _Refused("timeout must be finite")
    if timeout <= 0.0 or timeout > _MAX_TIMEOUT_SECONDS:
        raise _Refused("timeout is outside its accepted bounds")
    snapshot = None
    if operation == "close":
        snapshot = _validate_snapshot(value["snapshot"])
        target = snapshot["target"]
        dialog = snapshot["dialog"]
        if set(target) != set(("pid", "display", "ciw_window")):
            raise _Refused("snapshot target fields are invalid")
        required_dialog = set(("window_id", "title", "mapped", "modal", "ownership"))
        if not isinstance(dialog, dict) or not required_dialog.issubset(set(dialog)):
            raise _Refused("snapshot dialog fields are invalid")
        source = {
            "pid": target["pid"], "display": target["display"],
            "ciw_window": target["ciw_window"], "window_id": dialog["window_id"],
            "title": dialog["title"],
        }
    else:
        source = value
    pid = source["pid"]
    if isinstance(pid, bool) or not isinstance(pid, _INTEGER_TYPES) or pid <= 0:
        raise _Refused("pid must be a positive integer")
    display = _require_text(source["display"], "display", _MAX_DISPLAY_CHARS)
    if not _DISPLAY_RE.match(display):
        raise _Refused("display has an invalid form")
    title = _require_text(source["title"], "title", _MAX_TITLE_CHARS)
    command = {
        "op": operation,
        "pid": int(pid),
        "display": display,
        "ciw_window": _canonical_xid(source["ciw_window"], "ciw_window"),
        "window_id": _canonical_xid(source["window_id"], "window_id"),
        "title": title,
        "timeout": timeout,
    }
    if operation == "close":
        command["snapshot"] = snapshot
    return command


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise _Refused("snapshot must be an object")
    expected = set((
        "version", "target", "dialog", "process_start_ticks",
        "native", "content_sha256",
    ))
    if set(snapshot) != expected or snapshot.get("version") != 1:
        raise _Refused("snapshot fields do not match the supported schema")
    if not isinstance(snapshot["target"], dict):
        raise _Refused("snapshot target must be an object")
    if not isinstance(snapshot["dialog"], dict):
        raise _Refused("snapshot dialog must be an object")
    ticks = snapshot["process_start_ticks"]
    if isinstance(ticks, bool) or not isinstance(ticks, _INTEGER_TYPES) or ticks <= 0:
        raise _Refused("snapshot process_start_ticks is invalid")
    native = snapshot["native"]
    native_keys = set((
        "window_id", "mapped", "title", "pid", "wm_class", "wm_protocols",
        "wm_state", "transient_for", "client_leader", "client_machine", "geometry",
    ))
    if not isinstance(native, dict) or set(native) != native_keys:
        raise _Refused("snapshot native metadata is invalid")
    _canonical_xid(native["window_id"], "snapshot native window_id")
    if native["mapped"] is not True:
        raise _Refused("snapshot native window is not mapped")
    _require_text(native["title"], "snapshot native title", _MAX_TITLE_CHARS, allow_empty=True)
    if isinstance(native["pid"], bool) or not isinstance(native["pid"], _INTEGER_TYPES) or native["pid"] <= 0:
        raise _Refused("snapshot native pid is invalid")
    if not isinstance(native["wm_class"], list) or len(native["wm_class"]) > 2:
        raise _Refused("snapshot native wm_class is invalid")
    for item in native["wm_class"]:
        _require_text(item, "snapshot native wm_class item", 1024, allow_empty=True)
    if not isinstance(native["wm_protocols"], list) or len(native["wm_protocols"]) > 256:
        raise _Refused("snapshot native wm_protocols is invalid")
    for item in native["wm_protocols"]:
        _require_text(item, "snapshot native protocol", 256)
    if not isinstance(native["wm_state"], list) or len(native["wm_state"]) > 256:
        raise _Refused("snapshot native wm_state is invalid")
    for item in native["wm_state"]:
        _require_text(item, "snapshot native wm_state item", 256)
    for name in ("transient_for", "client_leader"):
        if native[name] is not None:
            _canonical_xid(native[name], "snapshot native %s" % name)
    if native["client_machine"] is not None:
        _require_text(native["client_machine"], "snapshot native client_machine", 1024)
    geometry = native["geometry"]
    if not isinstance(geometry, dict) or set(geometry) != set(("width", "height", "depth", "bits_per_pixel", "bytes_per_line")):
        raise _Refused("snapshot native geometry is invalid")
    for name in geometry:
        number = geometry[name]
        if isinstance(number, bool) or not isinstance(number, _INTEGER_TYPES) or number <= 0:
            raise _Refused("snapshot native geometry is invalid")
    digest = snapshot["content_sha256"]
    if not isinstance(digest, _TEXT_TYPES) or not _SHA256_RE.match(digest):
        raise _Refused("snapshot content_sha256 is invalid")
    return snapshot


def _read_process_start_ticks(pid):
    path = "/proc/%d/stat" % int(pid)
    try:
        with open(path, "rb") as handle:
            value = handle.read(8193)
    except (IOError, OSError) as error:
        raise _Refused("cannot read process start time: %s" % error)
    if len(value) > 8192:
        raise _Refused("process stat record exceeds bound")
    if not isinstance(value, str):
        value = value.decode("ascii", "strict")
    closing = value.rfind(")")
    if closing < 0:
        raise _Refused("process stat record is malformed")
    fields = value[closing + 2:].split()
    if len(fields) <= 19:
        raise _Refused("process stat record is incomplete")
    try:
        ticks = int(fields[19])
    except ValueError:
        raise _Refused("process start time is malformed")
    if ticks <= 0:
        raise _Refused("process start time is invalid")
    return ticks


def _inspect_exact(command):
    try:
        report = _inventory.inspect_dialogs(
            command["pid"], display=command["display"],
            ciw_window=command["ciw_window"], timeout=command["timeout"],
        )
    except Exception as error:
        raise _Refused("dialog inspection failed: %s" % error)
    if not isinstance(report, dict) or report.get("status") != "blocked":
        raise _Refused("target inspection is not blocked")
    expected_target = {
        "pid": command["pid"], "display": command["display"],
        "ciw_window": command["ciw_window"],
    }
    if report.get("target") != expected_target:
        raise _Refused("inspection target does not match the command")
    dialogs = report.get("dialogs")
    if not isinstance(dialogs, list) or len(dialogs) != 1:
        raise _Refused("inspection did not find exactly one dialog")
    dialog = dialogs[0]
    if not isinstance(dialog, dict):
        raise _Refused("inspection dialog is malformed")
    if dialog.get("ownership") != "target" or dialog.get("mapped") is not True or dialog.get("modal") is not True:
        raise _Refused("dialog is not a mapped target-owned modal")
    if _canonical_xid(dialog.get("window_id"), "inspection window_id") != command["window_id"]:
        raise _Refused("inspection window id does not match the command")
    if dialog.get("title") != command["title"]:
        raise _Refused("inspection title does not match the command")
    return report["target"], dialog


def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def _mask_channel(pixel, mask):
    if not mask:
        return 0
    shift = 0
    shifted = int(mask)
    while shifted & 1 == 0:
        shifted >>= 1
        shift += 1
    maximum = shifted
    value = (pixel & int(mask)) >> shift
    return (value * 255 + maximum // 2) // maximum


def _preview_png(image, raw):
    width = int(image.width)
    height = int(image.height)
    # Approval must preserve readable text; capture already enforces 4 MiB.
    out_width = width
    out_height = height
    pixel_bytes = (int(image.bits_per_pixel) + 7) // 8
    if pixel_bytes not in (2, 3, 4):
        raise _Refused("unsupported XImage pixel format")
    byte_order = "little" if int(image.byte_order) == _LSB_FIRST else "big"
    scanlines = []
    for out_y in range(out_height):
        source_y = out_y
        row = bytearray((0,))
        for out_x in range(out_width):
            source_x = out_x
            offset = source_y * int(image.bytes_per_line) + source_x * pixel_bytes
            sample = raw[offset:offset + pixel_bytes]
            if sys.version_info[0] < 3:
                pixel = 0
                ordered = sample if byte_order == "little" else sample[::-1]
                for index, character in enumerate(ordered):
                    pixel |= ord(character) << (index * 8)
            else:
                pixel = int.from_bytes(sample, byte_order)
            row.extend((
                _mask_channel(pixel, image.red_mask),
                _mask_channel(pixel, image.green_mask),
                _mask_channel(pixel, image.blue_mask),
            ))
        scanlines.append(bytes(row))
    pixels = b"".join(scanlines)
    header = struct.pack(">IIBBBBB", out_width, out_height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(pixels, 9)) + _png_chunk(b"IEND", b"")


class _NativeDialogConnection(object):
    def __init__(self, display, process_env):
        path = ctypes.util.find_library("X11")
        if not path:
            raise _Refused("libX11 not found")
        self._xlib = ctypes.cdll.LoadLibrary(path)
        self._display = None
        self._closed = False
        self._grabbed = False
        self._declare_signatures()
        previous = {}
        missing = object()
        for key in ("DISPLAY", "XAUTHORITY"):
            previous[key] = os.environ.get(key, missing)
        try:
            os.environ["DISPLAY"] = display
            authority = process_env.get("XAUTHORITY")
            if authority:
                os.environ["XAUTHORITY"] = authority
            else:
                os.environ.pop("XAUTHORITY", None)
            encoded = display.encode("utf-8")
            self._display = self._xlib.XOpenDisplay(encoded)
        finally:
            for key, old_value in previous.items():
                if old_value is missing:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value
        if not self._display:
            raise _Refused("cannot open display %s" % display)

    def _declare_signatures(self):
        lib = self._xlib
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.XInternAtom.restype = ctypes.c_ulong
        lib.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_inventory._XWindowAttributes)]
        lib.XGetWindowAttributes.restype = ctypes.c_int
        lib.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
        lib.XGetWindowProperty.restype = ctypes.c_int
        lib.XGetWMProtocols.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)), ctypes.POINTER(ctypes.c_int)]
        lib.XGetWMProtocols.restype = ctypes.c_int
        lib.XGetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XClassHint)]
        lib.XGetClassHint.restype = ctypes.c_int
        lib.XGetAtomName.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XGetAtomName.restype = ctypes.c_void_p
        lib.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
        lib.XGetImage.restype = ctypes.POINTER(_XImage)
        lib.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
        lib.XDestroyImage.restype = ctypes.c_int
        lib.XGrabServer.argtypes = [ctypes.c_void_p]
        lib.XUngrabServer.argtypes = [ctypes.c_void_p]
        lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(_XEvent)]
        lib.XSendEvent.restype = ctypes.c_int
        lib.XFree.argtypes = [ctypes.c_void_p]
        lib.XFree.restype = ctypes.c_int

    def _atom(self, name):
        return int(self._xlib.XInternAtom(self._display, name.encode("ascii"), 0))

    def _atom_name(self, atom):
        pointer = self._xlib.XGetAtomName(self._display, int(atom))
        if not pointer:
            raise _Refused("cannot resolve WM protocol atom")
        try:
            raw = ctypes.string_at(pointer)
            return raw.decode("ascii", "strict")
        finally:
            self._xlib.XFree(pointer)

    def _property(self, window, name):
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        item_count = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        pointer = ctypes.c_void_p()
        status = self._xlib.XGetWindowProperty(
            self._display, int(window), self._atom(name), 0, 4096, 0, 0,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(item_count), ctypes.byref(bytes_after), ctypes.byref(pointer),
        )
        if status != 0 or bytes_after.value != 0:
            if pointer:
                self._xlib.XFree(pointer)
            raise _Refused("cannot read bounded %s property" % name)
        return actual_type.value, actual_format.value, item_count.value, pointer

    def _text_property(self, window, name):
        _property_type, fmt, count, pointer = self._property(window, name)
        try:
            if not pointer or fmt == 0:
                return None
            if fmt != 8 or count > 4096:
                raise _Refused("%s property has an invalid format" % name)
            raw = ctypes.string_at(pointer, count).split(b"\0", 1)[0]
            try:
                return raw.decode("utf-8", "strict")
            except UnicodeDecodeError:
                return raw.decode("latin-1", "strict")
        finally:
            if pointer:
                self._xlib.XFree(pointer)

    def _pid_property(self, window):
        property_type, fmt, count, pointer = self._property(window, "_NET_WM_PID")
        try:
            if not pointer or property_type != _XA_CARDINAL or fmt != 32 or count != 1:
                raise _Refused("native window PID property is absent or invalid")
            return int(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong))[0])
        finally:
            if pointer:
                self._xlib.XFree(pointer)

    def _window_property(self, window, name):
        property_type, fmt, count, pointer = self._property(window, name)
        try:
            if not pointer or fmt == 0:
                return None
            if property_type != _XA_WINDOW or fmt != 32 or count != 1:
                raise _Refused("%s property is invalid" % name)
            value = int(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong))[0])
            return _inventory._normalize_xid(value) if value else None
        finally:
            if pointer:
                self._xlib.XFree(pointer)

    def _atom_list_property(self, window, name):
        property_type, fmt, count, pointer = self._property(window, name)
        try:
            if not pointer or fmt == 0:
                return []
            if property_type != _XA_ATOM or fmt != 32 or count > 256:
                raise _Refused("%s property is invalid or exceeds bound" % name)
            atoms = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong))
            return sorted(self._atom_name(atoms[index]) for index in range(count))
        finally:
            if pointer:
                self._xlib.XFree(pointer)

    def _class_hint(self, window):
        hint = _XClassHint()
        if not self._xlib.XGetClassHint(self._display, int(window), ctypes.byref(hint)):
            return []
        values = []
        try:
            for pointer in (hint.res_name, hint.res_class):
                values.append(ctypes.string_at(pointer).decode("utf-8", "replace") if pointer else "")
        finally:
            if hint.res_name:
                self._xlib.XFree(hint.res_name)
            if hint.res_class:
                self._xlib.XFree(hint.res_class)
        return values

    def _protocols(self, window):
        pointer = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_int()
        if not self._xlib.XGetWMProtocols(self._display, int(window), ctypes.byref(pointer), ctypes.byref(count)):
            return []
        try:
            if count.value < 0 or count.value > 256:
                raise _Refused("WM_PROTOCOLS exceeds bound")
            return sorted(self._atom_name(pointer[index]) for index in range(count.value))
        finally:
            if pointer:
                self._xlib.XFree(ctypes.cast(pointer, ctypes.c_void_p))

    def capture(self, window_id, make_preview=False):
        window = int(window_id, 16)
        attributes = _inventory._XWindowAttributes()
        if not self._xlib.XGetWindowAttributes(self._display, window, ctypes.byref(attributes)):
            raise _Refused("cannot read native window attributes")
        if attributes.map_state != _IS_VIEWABLE or attributes.width <= 0 or attributes.height <= 0:
            raise _Refused("native window is not viewable")
        if int(attributes.width) * int(attributes.height) * 4 > _MAX_IMAGE_BYTES:
            raise _Refused("native window capture exceeds 4 MiB bound")
        image_pointer = self._xlib.XGetImage(
            self._display, window, 0, 0, attributes.width, attributes.height,
            ctypes.c_ulong(-1).value, _ZPIXMAP,
        )
        if not image_pointer:
            raise _Refused("XGetImage failed")
        try:
            image = image_pointer.contents
            byte_count = int(image.bytes_per_line) * int(image.height)
            if byte_count <= 0 or byte_count > _MAX_IMAGE_BYTES or not image.data:
                raise _Refused("XGetImage result exceeds bound")
            raw = ctypes.string_at(image.data, byte_count)
            title = self._text_property(window, "_NET_WM_NAME")
            if title is None:
                title = self._text_property(window, "WM_NAME")
            if title is None:
                title = ""
            native = {
                "window_id": _inventory._normalize_xid(window),
                "mapped": True,
                "title": title,
                "pid": self._pid_property(window),
                "wm_class": self._class_hint(window),
                "wm_protocols": self._protocols(window),
                "wm_state": self._atom_list_property(window, "_NET_WM_STATE"),
                "transient_for": self._window_property(window, "WM_TRANSIENT_FOR"),
                "client_leader": self._window_property(window, "WM_CLIENT_LEADER"),
                "client_machine": self._text_property(window, "WM_CLIENT_MACHINE"),
                "geometry": {
                    "width": int(image.width), "height": int(image.height),
                    "depth": int(image.depth), "bits_per_pixel": int(image.bits_per_pixel),
                    "bytes_per_line": int(image.bytes_per_line),
                },
            }
            result = {"native": native, "content_sha256": hashlib.sha256(raw).hexdigest()}
            if make_preview:
                result["preview_png_b64"] = base64.b64encode(_preview_png(image, raw)).decode("ascii")
            return result
        finally:
            self._xlib.XDestroyImage(image_pointer)

    def grab(self):
        if self._grabbed:
            raise _Refused("X server is already grabbed")
        self._xlib.XGrabServer(self._display)
        self._grabbed = True

    def ungrab(self):
        if self._grabbed:
            self._xlib.XUngrabServer(self._display)
            self._xlib.XSync(self._display, 0)
            self._grabbed = False

    def send_delete(self, window_id):
        event = _XEvent()
        event.client.type = _CLIENT_MESSAGE
        event.client.display = self._display
        event.client.window = int(window_id, 16)
        event.client.message_type = self._atom("WM_PROTOCOLS")
        event.client.format = 32
        event.client.data.l[0] = self._atom("WM_DELETE_WINDOW")
        event.client.data.l[1] = _CURRENT_TIME
        status = self._xlib.XSendEvent(
            self._display, event.client.window, 0, _NO_EVENT_MASK,
            ctypes.byref(event),
        )
        self._xlib.XSync(self._display, 0)
        if not status:
            raise _PossibleTransmission("XSendEvent did not confirm queuing")

    def close(self):
        if self._closed:
            return
        try:
            self.ungrab()
        finally:
            if self._display:
                self._xlib.XCloseDisplay(self._display)
            self._closed = True


def _open_native_connection(pid, display):
    try:
        process_env = _inventory._read_process_x11_env(pid)
    except Exception as error:
        raise _Refused("cannot read process X11 environment: %s" % error)
    if process_env.get("DISPLAY") != display:
        raise _Refused("process DISPLAY changed")
    return _NativeDialogConnection(display, process_env)


def _require_native_match(capture, snapshot, command):
    if not isinstance(capture, dict) or set(capture) - set(("native", "content_sha256", "preview_png_b64")):
        raise _Refused("native capture result is malformed")
    if capture.get("native") != snapshot["native"]:
        raise _Refused("native window metadata changed")
    if capture.get("content_sha256") != snapshot["content_sha256"]:
        raise _Refused("native window content changed")
    native = capture["native"]
    if native.get("window_id") != command["window_id"] or native.get("pid") != command["pid"] or native.get("title") != command["title"]:
        raise _Refused("native target identity does not match the command")
    if "WM_DELETE_WINDOW" not in native.get("wm_protocols", []):
        raise _Refused("target does not support WM_DELETE_WINDOW")


def _prepare(command):
    target, dialog = _inspect_exact(command)
    ticks = _read_process_start_ticks(command["pid"])
    connection = _open_native_connection(command["pid"], command["display"])
    try:
        capture = connection.capture(command["window_id"], make_preview=True)
    finally:
        connection.close()
    native = capture.get("native")
    if not isinstance(native, dict):
        raise _Refused("native capture result is malformed")
    if native.get("window_id") != command["window_id"] or native.get("pid") != command["pid"] or native.get("title") != command["title"]:
        raise _Refused("native target identity does not match the command")
    if native.get("mapped") is not True or "WM_DELETE_WINDOW" not in native.get("wm_protocols", []):
        raise _Refused("target is not mapped or lacks WM_DELETE_WINDOW")
    snapshot = {
        "version": 1,
        "target": target,
        "dialog": dialog,
        "process_start_ticks": ticks,
        "native": native,
        "content_sha256": capture.get("content_sha256"),
    }
    _validate_snapshot(snapshot)
    preview = capture.get("preview_png_b64")
    if not isinstance(preview, _TEXT_TYPES) or len(preview) > _MAX_IMAGE_BYTES * 2:
        raise _Refused("preview PNG is missing or exceeds bound")
    return {"status": "prepared", "snapshot": snapshot, "preview_png_b64": preview}


def _close(command):
    snapshot = command["snapshot"]
    target, dialog = _inspect_exact(command)
    if target != snapshot["target"] or dialog != snapshot["dialog"]:
        raise _Refused("inspection state changed after preparation")
    if _read_process_start_ticks(command["pid"]) != snapshot["process_start_ticks"]:
        raise _Refused("target process instance changed")
    connection = _open_native_connection(command["pid"], command["display"])
    attempted = False
    try:
        _require_native_match(connection.capture(command["window_id"]), snapshot, command)
        timer = _grab_exit_timer(command["timeout"])
        timer.arm()
        try:
            connection.grab()
            try:
                _require_native_match(connection.capture(command["window_id"]), snapshot, command)
                attempted = True
                connection.send_delete(command["window_id"])
            except _PossibleTransmission:
                raise
            except Exception as error:
                if attempted:
                    raise _PossibleTransmission(str(error))
                raise
            finally:
                try:
                    connection.ungrab()
                except Exception as error:
                    if attempted:
                        raise _PossibleTransmission(str(error))
                    raise
        finally:
            try:
                timer.cancel()
            except Exception as error:
                if attempted:
                    raise _PossibleTransmission(str(error))
                raise
    finally:
        try:
            connection.close()
        except Exception as error:
            if attempted:
                raise _PossibleTransmission(str(error))
            raise
    return {"status": "requested", "action_sent": True}


def handle_command(value):
    try:
        command = _validate_command(value)
        if command["op"] == "prepare":
            return _prepare(command)
        return _close(command)
    except _PossibleTransmission as error:
        return {"status": "unknown", "action_sent": None, "diagnostic": str(error)}
    except Exception as error:
        return {"status": "not_started", "action_sent": False, "diagnostic": str(error)}


def _read_command(stream):
    raw = stream.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        raise _Refused("JSON command exceeds 64 KiB")
    if not raw:
        raise _Refused("JSON command is empty")
    if not isinstance(raw, str):
        raw = raw.decode("utf-8", "strict")
    return json.loads(raw)


def main():
    try:
        command = _read_command(getattr(sys.stdin, "buffer", sys.stdin))
        result = handle_command(command)
    except Exception as error:
        result = {"status": "not_started", "action_sent": False, "diagnostic": str(error)}
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(encoded + "\n")
    return 0 if result.get("status") in ("prepared", "requested") else 2


if __name__ == "__main__":
    sys.exit(main())

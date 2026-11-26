import base64
import ctypes
import importlib.util
import io
import pathlib
import struct
from types import SimpleNamespace

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "virtuoso_bridge" / "resources" / "x11_dialog_close.py"
SPEC = importlib.util.spec_from_file_location("x11_dialog_close", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


PID = 4321
DISPLAY = ":19"
CIW = "0x100"
WINDOW = "0x200"
TITLE = "Confirm close"


def inspection(dialogs=None):
    if dialogs is None:
        dialogs = [{
            "window_id": WINDOW,
            "dismiss_id": WINDOW,
            "frame_id": WINDOW,
            "title": TITLE,
            "class": ["Virtuoso", "Cadence"],
            "geometry": {"x": 1, "y": 2, "width": 300, "height": 120},
            "mapped": True,
            "modal": True,
            "kind": "known_modal",
            "suggested_action": None,
            "ownership": "target",
            "source": "unknown",
        }]
    return {
        "status": "blocked" if dialogs else "clear",
        "target": {"pid": PID, "display": DISPLAY, "ciw_window": CIW},
        "dialogs": dialogs,
        "diagnostics": [],
    }


def native(content="a" * 64, pid=PID, window=WINDOW, title=TITLE, protocols=None):
    return {
        "native": {
            "window_id": window,
            "mapped": True,
            "title": title,
            "pid": pid,
            "wm_class": ["Virtuoso", "Cadence"],
            "wm_protocols": list(protocols if protocols is not None else ["WM_DELETE_WINDOW", "WM_TAKE_FOCUS"]),
            "wm_state": ["_NET_WM_STATE_MODAL"],
            "transient_for": CIW,
            "client_leader": CIW,
            "client_machine": "localhost",
            "visual": {"visual_id": 42, "visual_class": 4, "red_mask": 0xFF0000,
                       "green_mask": 0xFF00, "blue_mask": 0xFF},
            "geometry": {"width": 300, "height": 120, "depth": 24, "bits_per_pixel": 32, "bytes_per_line": 1200},
        },
        "content_sha256": content,
    }


class FakeConnection:
    def __init__(self, captures, send_error=None, ungrab_error=None):
        self.captures = list(captures)
        self.send_error = send_error
        self.ungrab_error = ungrab_error
        self.grabbed = False
        self.grab_calls = 0
        self.ungrab_calls = 0
        self.send_calls = 0
        self.closed = False

    def capture(self, window_id, make_preview=False):
        assert window_id == WINDOW
        value = dict(self.captures.pop(0))
        if make_preview:
            value["preview_png_b64"] = base64.b64encode(b"\x89PNG\r\n\x1a\npreview").decode("ascii")
        return value

    def grab(self):
        assert not self.grabbed
        self.grabbed = True
        self.grab_calls += 1

    def ungrab(self):
        if self.grabbed:
            self.grabbed = False
            self.ungrab_calls += 1
            if self.ungrab_error:
                raise self.ungrab_error

    def send_delete(self, window_id):
        assert self.grabbed
        assert window_id == WINDOW
        self.send_calls += 1
        if self.send_error:
            raise self.send_error

    def close(self):
        self.closed = True


def command(op="prepare", snapshot=None):
    if op == "close":
        return {"op": "close", "snapshot": snapshot, "timeout": 5.0}
    return {
        "op": "prepare", "pid": PID, "display": DISPLAY,
        "ciw_window": CIW, "window_id": WINDOW, "title": TITLE,
        "timeout": 5.0,
    }


def install(monkeypatch, fake, report=None, ticks=777):
    report = inspection() if report is None else report

    def inspect(*args, **kwargs):
        assert not fake.grabbed
        assert args == (PID,)
        assert kwargs == {"display": DISPLAY, "ciw_window": CIW, "timeout": 5.0}
        return report

    monkeypatch.setattr(MODULE._inventory, "inspect_dialogs", inspect)
    monkeypatch.setattr(MODULE, "_read_process_start_ticks", lambda pid: ticks)
    monkeypatch.setattr(MODULE, "_open_native_connection", lambda pid, display: fake)

    class Timer:
        def arm(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(MODULE, "_grab_exit_timer", lambda seconds: Timer())


def prepared(monkeypatch):
    capture = native()
    fake = FakeConnection([capture])
    install(monkeypatch, fake)
    result = MODULE.handle_command(command())
    assert result["status"] == "prepared"
    assert result["snapshot"]["target"] == inspection()["target"]
    assert result["snapshot"]["dialog"] == inspection()["dialogs"][0]
    assert result["snapshot"]["process_start_ticks"] == 777
    assert result["snapshot"]["native"] == capture["native"]
    assert result["snapshot"]["content_sha256"] == "a" * 64
    assert base64.b64decode(result["preview_png_b64"]).startswith(b"\x89PNG")
    assert fake.closed
    return result["snapshot"]


def test_prepare_captures_exact_owned_modal(monkeypatch):
    prepared(monkeypatch)


def test_close_rechecks_under_grab_and_sends_once(monkeypatch):
    snapshot = prepared(monkeypatch)
    fake = FakeConnection([native(), native()])
    install(monkeypatch, fake)
    result = MODULE.handle_command(command("close", snapshot))
    assert result == {"status": "requested", "action_sent": True}
    assert fake.grab_calls == 1
    assert fake.ungrab_calls == 1
    assert fake.send_calls == 1
    assert fake.closed


def test_helper_exit_timer_brackets_server_grab(monkeypatch):
    snapshot = prepared(monkeypatch)
    fake = FakeConnection([native(), native()])
    events = []

    class Timer:
        def arm(self):
            events.append("arm")

        def cancel(self):
            events.append("cancel")

    install(monkeypatch, fake)
    monkeypatch.setattr(MODULE, "_grab_exit_timer", lambda seconds: Timer())
    original_grab = fake.grab
    original_ungrab = fake.ungrab

    def grab():
        events.append("grab")
        original_grab()

    def ungrab():
        events.append("ungrab")
        original_ungrab()

    fake.grab = grab
    fake.ungrab = ungrab
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "requested"
    assert events == ["arm", "grab", "ungrab", "cancel"]


def test_grab_timer_uses_kernel_default_signal_disposition(monkeypatch):
    calls = []
    previous = object()
    monkeypatch.setattr(MODULE.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(MODULE.signal, "ITIMER_REAL", 0, raising=False)
    monkeypatch.setattr(
        MODULE.signal, "signal",
        lambda signum, handler: calls.append(("signal", signum, handler)) or previous,
    )
    monkeypatch.setattr(
        MODULE.signal, "setitimer",
        lambda which, seconds: calls.append(("timer", which, seconds)),
        raising=False,
    )
    timer = MODULE._GrabExitTimer(3.0)
    timer.arm()
    timer.cancel()
    assert calls == [
        ("signal", 14, MODULE.signal.SIG_DFL),
        ("timer", 0, 1.0),
        ("timer", 0, 0.0),
        ("signal", 14, previous),
    ]


@pytest.mark.parametrize(
    "changed",
    [
        native(content="b" * 64),
        native(pid=PID + 1),
        native(window="0x201"),
        native(title="Different title"),
    ],
    ids=["content", "pid", "window-id", "title"],
)
def test_close_refuses_stale_native_state_before_send(monkeypatch, changed):
    snapshot = prepared(monkeypatch)
    fake = FakeConnection([changed])
    install(monkeypatch, fake)
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "not_started"
    assert result["action_sent"] is False
    assert fake.send_calls == 0
    assert fake.grab_calls == 0


def test_post_send_timer_cleanup_failure_is_unknown(monkeypatch):
    snapshot = prepared(monkeypatch)
    fake = FakeConnection([native(), native()])
    install(monkeypatch, fake)
    class BrokenTimer:
        def arm(self):
            pass
        def cancel(self):
            raise OSError("timer cleanup failed")
    monkeypatch.setattr(MODULE, "_grab_exit_timer", lambda seconds: BrokenTimer())
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "unknown" and result["action_sent"] is None
    assert fake.send_calls == 1 and fake.closed and not fake.grabbed


def test_close_refuses_stale_process_instance(monkeypatch):
    snapshot = prepared(monkeypatch)
    fake = FakeConnection([])
    install(monkeypatch, fake, ticks=778)
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "not_started"
    assert result["action_sent"] is False
    assert fake.send_calls == 0


def test_close_refuses_multiple_dialogs(monkeypatch):
    snapshot = prepared(monkeypatch)
    extra = dict(inspection()["dialogs"][0])
    extra["window_id"] = "0x201"
    report = inspection(inspection()["dialogs"] + [extra])
    fake = FakeConnection([])
    install(monkeypatch, fake, report=report)
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "not_started"
    assert result["action_sent"] is False
    assert fake.send_calls == 0


def test_close_refuses_missing_delete_protocol(monkeypatch):
    snapshot = prepared(monkeypatch)
    fake = FakeConnection([native(protocols=["WM_TAKE_FOCUS"])])
    install(monkeypatch, fake)
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "not_started"
    assert result["action_sent"] is False
    assert fake.send_calls == 0


def test_close_refuses_change_during_final_grab(monkeypatch):
    snapshot = prepared(monkeypatch)
    fake = FakeConnection([native(), native(content="c" * 64)])
    install(monkeypatch, fake)
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "not_started"
    assert result["action_sent"] is False
    assert fake.send_calls == 0
    assert fake.ungrab_calls == 1


def test_send_exception_is_unknown_and_not_retried(monkeypatch):
    snapshot = prepared(monkeypatch)
    fake = FakeConnection([native(), native()], send_error=RuntimeError("send failed"))
    install(monkeypatch, fake)
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "unknown"
    assert result["action_sent"] is None
    assert fake.send_calls == 1
    assert fake.ungrab_calls == 1


def test_ungrab_exception_after_send_is_unknown(monkeypatch):
    snapshot = prepared(monkeypatch)
    fake = FakeConnection([native(), native()], ungrab_error=RuntimeError("ungrab failed"))
    install(monkeypatch, fake)
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "unknown"
    assert result["action_sent"] is None
    assert fake.send_calls == 1


@pytest.mark.parametrize("field,value", [("title", "bad\nvalue"), ("timeout", 0), ("extra", True)])
def test_command_schema_fails_closed(field, value):
    value_command = command()
    value_command[field] = value
    result = MODULE.handle_command(value_command)
    assert result["status"] == "not_started"
    assert result["action_sent"] is False


def test_stdin_is_bounded_to_64_kib():
    with pytest.raises(MODULE._Refused, match="64 KiB"):
        MODULE._read_command(io.BytesIO(b" " * (64 * 1024 + 1)))


def test_preview_png_is_generated_from_bounded_ximage_pixels():
    image = MODULE._XImage()
    image.width = 2
    image.height = 1
    image.bytes_per_line = 8
    image.bits_per_pixel = 32
    image.byte_order = MODULE._LSB_FIRST
    image.red_mask = 0x00FF0000
    image.green_mask = 0x0000FF00
    image.blue_mask = 0x000000FF
    png = MODULE._preview_png(image, b"\x00\x00\xff\x00\x00\xff\x00\x00")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png.endswith(b"IEND\xaeB`\x82")


def test_preview_preserves_readable_native_dialog_dimensions():
    image = MODULE._XImage()
    image.width = 477
    image.height = 83
    image.bytes_per_line = 477 * 4
    image.bits_per_pixel = 32
    image.byte_order = MODULE._LSB_FIRST
    image.red_mask = 0x00FF0000
    image.green_mask = 0x0000FF00
    image.blue_mask = 0x000000FF
    png = MODULE._preview_png(image, b"\xff" * (image.bytes_per_line * image.height))
    assert struct.unpack(">II", png[16:24]) == (477, 83)


def test_backend_contains_no_keyboard_focus_or_force_close_primitives():
    source = MODULE_PATH.read_text(encoding="ascii")
    for forbidden in ("XTest", "XSetInputFocus", "XDestroyWindow", "XKillClient", "XWarpPointer", "XKeyEvent"):
        assert forbidden not in source
    assert source.count("XSendEvent(") == 1


@pytest.mark.parametrize("left,right,overlap", [
    ((0, 0, 10, 10), (10, 0, 20, 10), False),
    ((0, 0, 10, 10), (9, 9, 20, 20), True),
    ((0, 0, 10, 10), (0, 10, 10, 20), False),
])
def test_visibility_rectangles_do_not_treat_touching_edges_as_occlusion(left, right, overlap):
    assert MODULE._intersects(left, right) is overlap
    assert MODULE._contains(left, left)
    assert not MODULE._contains(left, (-1, 0, 10, 10))


@pytest.mark.parametrize("errors,status,missing", [([3], 0, True), ([10], 0, False),
                                                        ([3, 10], 0, False), ([], 0, False)])
def test_only_exact_badwindow_proves_absence(errors, status, missing):
    connection = object.__new__(MODULE._NativeDialogConnection)
    connection._display = None

    def attributes(*args):
        connection._errors.extend(errors)
        return status

    connection._xlib = SimpleNamespace(XGetWindowAttributes=attributes, XSync=lambda *a: None)
    if missing:
        assert connection._attributes(42, allow_missing=True) is None
    else:
        with pytest.raises(MODULE._Refused):
            connection._attributes(42, allow_missing=True)


def test_preview_encoding_runs_after_owned_grab_is_released(monkeypatch):
    connection = object.__new__(MODULE._NativeDialogConnection)
    connection._grabbed = False
    connection._load_shape_library = lambda: None
    events = []
    connection.grab = lambda: events.append("grab")
    connection.ungrab = lambda: events.append("ungrab")
    connection._capture_locked = lambda *a: {"_preview": (object(), b"pixels")}
    monkeypatch.setattr(MODULE, "_grab_exit_timer", lambda *a: SimpleNamespace(
        arm=lambda: events.append("arm"), cancel=lambda: events.append("cancel")))

    def preview(*args):
        assert events == ["arm", "grab", "ungrab", "cancel"]
        return b"PNG"

    monkeypatch.setattr(MODULE, "_preview_png", preview)
    assert connection.capture(WINDOW, make_preview=True) == {"preview_png_b64": "UE5H"}


def test_capture_refusal_releases_owned_grab(monkeypatch):
    connection = object.__new__(MODULE._NativeDialogConnection)
    connection._grabbed = False
    connection._load_shape_library = lambda: None
    events = []
    connection.grab = lambda: events.append("grab")
    connection.ungrab = lambda: events.append("ungrab")

    def refuse(*args):
        raise MODULE._Refused("obscured")

    connection._capture_locked = refuse
    monkeypatch.setattr(MODULE, "_grab_exit_timer", lambda *a: SimpleNamespace(
        arm=lambda: events.append("arm"), cancel=lambda: events.append("cancel")))
    with pytest.raises(MODULE._Refused, match="obscured"):
        connection.capture(WINDOW)
    assert events == ["arm", "grab", "ungrab", "cancel"]


def test_missing_shape_library_cannot_approve_pixels(monkeypatch):
    connection = object.__new__(MODULE._NativeDialogConnection)
    connection._shape = None
    connection._grabbed = False
    monkeypatch.setattr(MODULE.ctypes.util, "find_library", lambda *a: None)
    with pytest.raises(MODULE._Refused, match="libXext"):
        connection._load_shape_library()


def test_shape_library_lookup_cannot_spawn_probes_while_grabbed(monkeypatch):
    connection = object.__new__(MODULE._NativeDialogConnection)
    connection._shape = None
    connection._grabbed = True
    monkeypatch.setattr(MODULE.ctypes.util, "find_library", lambda *a: pytest.fail("probe while grabbed"))
    with pytest.raises(MODULE._Refused, match="before the server grab"):
        connection._load_shape_library()


def test_state_rejects_process_reuse_before_x11(monkeypatch):
    snapshot = prepared(monkeypatch)
    monkeypatch.setattr(MODULE, "_read_process_start_ticks", lambda *a: 778)

    def unexpected_connection(*args):
        pytest.fail("State query must not open reused process display")

    monkeypatch.setattr(MODULE, "_open_native_connection", unexpected_connection)
    result = MODULE.handle_command({"op": "state", "snapshot": snapshot, "timeout": 5.0})
    assert result["status"] == "not_started" and "instance changed" in result["diagnostic"]


@pytest.mark.parametrize("visual_class", [0, 1, 2, 3, 5])
def test_mutable_or_unsupported_visuals_cannot_approve_pixels(visual_class):
    connection = object.__new__(MODULE._NativeDialogConnection)
    visual = MODULE._XVisual()
    visual.visual_class = visual_class
    attrs = SimpleNamespace(visual=ctypes.cast(ctypes.pointer(visual), ctypes.c_void_p))
    with pytest.raises(MODULE._Refused, match="TrueColor"):
        connection._truecolor_visual(attrs)


def test_missing_native_visual_cannot_approve_pixels():
    connection = object.__new__(MODULE._NativeDialogConnection)
    with pytest.raises(MODULE._Refused, match="unavailable"):
        connection._truecolor_visual(SimpleNamespace(visual=None))


def test_changed_visual_refuses_close_even_if_pixel_indices_match(monkeypatch):
    snapshot = prepared(monkeypatch)
    changed = native()
    changed["native"]["visual"]["visual_id"] = 43
    fake = FakeConnection([changed])
    install(monkeypatch, fake)
    result = MODULE.handle_command(command("close", snapshot))
    assert result["status"] == "not_started"
    assert fake.send_calls == 0


def test_prepare_close_matches_virtuoso_registered_trademark_title_from_c_locale_xprop(monkeypatch):
    title = 'Virtuoso\u00ae "Save" Notice'

    class _FakeInventoryConnection:
        def root(self):
            return 1

        def children(self, window_id):
            return [0x100, 0x200] if window_id == 1 else []

        def attributes(self, window_id):
            sizes = {1: (1920, 1080), 0x100: (900, 200), 0x200: (300, 120)}
            w, h = sizes[window_id]
            return {
                "mapped": True,
                "window_class": 1,
                "depth": 24,
                "override_redirect": False,
                "geometry": {"x": 10, "y": 20, "w": w, "h": h},
            }

        def close(self):
            pass

    xprops = {
        "0x100": "\n".join([
            f"_NET_WM_PID(CARDINAL) = {PID}",
            'WM_CLIENT_MACHINE(STRING) = "eda-host"',
            'WM_NAME(STRING) = "Virtuoso? Studio IC23.1 - Log: /tmp/CDS.log"',
            r'_NET_WM_NAME(UTF8_STRING) = "Virtuoso\302\256 Studio IC23.1 - Log: /tmp/CDS.log"',
            'WM_CLASS(STRING) = "virtuoso", "Virtuoso"',
            "WM_STATE(WM_STATE): window state: Normal",
        ]),
        "0x200": "\n".join([
            f"_NET_WM_PID(CARDINAL) = {PID}",
            "WM_TRANSIENT_FOR(WINDOW): window id # 0x100",
            'WM_CLIENT_MACHINE(STRING) = "eda-host"',
            "_NET_WM_STATE(ATOM) = _NET_WM_STATE_MODAL",
            r'WM_NAME(STRING) = "Virtuoso? \"Save\" Notice"',
            r'_NET_WM_NAME(UTF8_STRING) = "Virtuoso\302\256 \"Save\" Notice"',
            'WM_CLASS(STRING) = "virtuoso", "Virtuoso"',
            "WM_STATE(WM_STATE): window state: Normal",
        ]),
    }

    monkeypatch.setattr(
        MODULE._inventory,
        "_read_process_x11_env",
        lambda _pid: {"DISPLAY": DISPLAY, "XAUTHORITY": None, "HOSTNAME": "eda-host"},
    )
    monkeypatch.setattr(MODULE._inventory, "_open_x11_inventory", lambda *_a: _FakeInventoryConnection())
    monkeypatch.setattr(
        MODULE._inventory,
        "_bounded_check_output",
        lambda cmd, env=None, timeout=None: xprops[cmd[2]],
    )
    monkeypatch.setattr(MODULE, "_read_process_start_ticks", lambda _pid: 777)
    fake = FakeConnection([native(title=title)])
    monkeypatch.setattr(MODULE, "_open_native_connection", lambda pid, display: fake)

    prepare_cmd = command()
    prepare_cmd["title"] = title
    result = MODULE.handle_command(prepare_cmd)

    assert result["status"] == "prepared", result
    assert result["snapshot"]["dialog"]["title"] == title
    assert result["snapshot"]["native"]["title"] == title


def test_native_capture_falls_back_to_wm_name_when_net_wm_name_is_empty():
    connection = object.__new__(MODULE._NativeDialogConnection)
    connection._display = 1
    connection._grabbed = True

    pixel_buf = ctypes.create_string_buffer(b"\xff" * 4)
    image = MODULE._XImage()
    image.width = 1
    image.height = 1
    image.bytes_per_line = 4
    image.bits_per_pixel = 32
    image.depth = 24
    image.data = ctypes.addressof(pixel_buf)
    image_ptr = ctypes.pointer(image)

    connection._attributes = lambda _win: SimpleNamespace(map_state=MODULE._IS_VIEWABLE, width=1, height=1)
    connection._require_pixels_observable = lambda _w, _a: {
        "visual_id": 42, "visual_class": 4, "red_mask": 0xFF0000, "green_mask": 0xFF00, "blue_mask": 0xFF,
    }
    connection._xlib = SimpleNamespace(
        XGetImage=lambda *a: image_ptr,
        XDestroyImage=lambda *a: 0,
    )
    connection._text_property = lambda _w, name: {
        "_NET_WM_NAME": "",
        "WM_NAME": "Fallback Title",
        "WM_CLIENT_MACHINE": "localhost",
    }[name]
    connection._pid_property = lambda _w: PID
    connection._class_hint = lambda _w: ["virtuoso", "Virtuoso"]
    connection._protocols = lambda _w: ["WM_DELETE_WINDOW"]
    connection._atom_list_property = lambda _w, _n: ["_NET_WM_STATE_MODAL"]
    connection._window_property = lambda _w, _n: CIW

    captured = connection._capture_locked(WINDOW, make_preview=False)
    assert captured["native"]["title"] == "Fallback Title"

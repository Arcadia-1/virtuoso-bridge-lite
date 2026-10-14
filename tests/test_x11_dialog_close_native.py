"""Real Xlib regressions on an owned Xvfb; also runnable with Python 2.7."""

import ctypes
import os
import select
import subprocess
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOURCES = os.path.join(ROOT, "src", "virtuoso_bridge", "resources")
sys.path.insert(0, RESOURCES)
SOURCE = os.path.join(RESOURCES, "x11_dialog_close.py")
try:
    import importlib.util
except ImportError:
    import imp
    MODULE = imp.load_source("native_close", SOURCE)
else:
    SPEC = importlib.util.spec_from_file_location("native_close", SOURCE)
    MODULE = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(MODULE)

XVFB = next((os.path.join(path, "Xvfb") for path in os.environ.get("PATH", "").split(os.pathsep)
             if os.path.isfile(os.path.join(path, "Xvfb"))
             and os.access(os.path.join(path, "Xvfb"), os.X_OK)), None)


class VisualInfo(ctypes.Structure):
    _fields_ = [("visual", ctypes.c_void_p), ("visualid", ctypes.c_ulong),
                ("screen", ctypes.c_int), ("depth", ctypes.c_int),
                ("visual_class", ctypes.c_int), ("red_mask", ctypes.c_ulong),
                ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong),
                ("colormap_size", ctypes.c_int), ("bits_per_rgb", ctypes.c_int)]


class SetWindowAttributes(ctypes.Structure):
    _fields_ = [("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
                ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
                ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
                ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
                ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
                ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("colormap", ctypes.c_ulong),
                ("cursor", ctypes.c_ulong)]


class Color(ctypes.Structure):
    _fields_ = [("pixel", ctypes.c_ulong), ("red", ctypes.c_ushort),
                ("green", ctypes.c_ushort), ("blue", ctypes.c_ushort),
                ("flags", ctypes.c_char), ("pad", ctypes.c_char)]


class Display(object):
    def __init__(self):
        self.process = None
        read_fd, write_fd = os.pipe()
        try:
            if hasattr(os, "set_inheritable"):
                os.set_inheritable(write_fd, True)
            with open(os.devnull, "r+b") as devnull:
                self.process = subprocess.Popen(
                    [XVFB, "-displayfd", str(write_fd), "-screen", "0", "800x600x24",
                     "-nolisten", "tcp", "-ac", "-noreset"], close_fds=False,
                    stdin=devnull, stdout=devnull, stderr=subprocess.PIPE)
            os.close(write_fd)
            write_fd = None
            if not select.select([read_fd], [], [], 10)[0]:
                raise RuntimeError("Dedicated Xvfb did not become ready")
            number = os.read(read_fd, 64).decode("ascii").strip()
            if not number.isdigit():
                raise RuntimeError("Dedicated Xvfb returned an invalid display number")
            self.name = ":" + number
            sys.stdout.write("Owned Xvfb pid=%d display=%s\n" % (self.process.pid, self.name))
            sys.stdout.flush()
        except Exception:
            self.close()
            raise
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def close(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            deadline = time.time() + 5
            while self.process.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            if self.process.poll() is None:
                self.process.kill()
        self.process.wait()
        self.process.stderr.close()


class Canvas(object):
    def __init__(self, display):
        self.display = display
        self.connection = MODULE._NativeDialogConnection(display, {})
        self.lib = self.connection._xlib
        self.d = self.connection._display
        self.lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.lib.XDefaultRootWindow.restype = ctypes.c_ulong
        self.root = self.lib.XDefaultRootWindow(self.d)
        self.lib.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                                ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                                                ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
        self.lib.XCreateSimpleWindow.restype = ctypes.c_ulong
        self.lib.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                            ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_void_p, ctypes.c_int]
        self.lib.XSetWMProtocols.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                           ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
        self.lib.XSetTransientForHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        for name in ("XMapWindow", "XUnmapWindow", "XDestroyWindow", "XClearWindow"):
            getattr(self.lib, name).argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.XSetWindowBackground.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        self.lib.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
        self.lib.XPending.argtypes = [ctypes.c_void_p]
        self.lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULE._XEvent)]
        self.covers = []
        self.frame = self.create(self.root, 20, 20, 500, 350)
        self.window = self.create(self.frame, 15, 25, 160, 60)
        self.property("_NET_WM_PID", 6, 32, (ctypes.c_ulong * 1)(os.getpid()), 1)
        title = ctypes.create_string_buffer(b"Native reviewed information")
        self.property("_NET_WM_NAME", self.connection._atom("UTF8_STRING"), 8, title, len(title.value))
        protocols = (ctypes.c_ulong * 1)(self.connection._atom("WM_DELETE_WINDOW"))
        self.lib.XSetWMProtocols(self.d, self.window, protocols, 1)
        self.lib.XSetTransientForHint(self.d, self.window, self.frame)
        self.sync()

    def property(self, name, kind, format_, data, count):
        self.lib.XChangeProperty(self.d, self.window, self.connection._atom(name),
                                 kind, format_, 0, ctypes.cast(data, ctypes.c_void_p), count)

    def create(self, parent, x, y, width, height):
        window = self.lib.XCreateSimpleWindow(self.d, parent, x, y, width, height, 2, 0, 0xFFFFFF)
        self.lib.XMapWindow(self.d, window)
        self.sync()
        return window

    def sync(self):
        self.lib.XSync(self.d, 0)

    def cover(self, width=160, nested=False, border_only=False):
        parent = self.frame if nested else self.root
        x, y = (15, 25) if nested else (37, 47)
        if border_only:
            x += 161
        window = self.create(parent, x, y, width, 60)
        self.covers.append(window)
        return window

    def redraw(self):
        self.lib.XSetWindowBackground(self.d, self.window, 0xFF0000)
        self.lib.XClearWindow(self.d, self.window)
        self.sync()

    def direct_color(self, parent, x=0, y=0, width=160, height=60):
        self.lib.XMatchVisualInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.POINTER(VisualInfo)]
        self.lib.XCreateColormap.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.c_void_p, ctypes.c_int]
        self.lib.XCreateColormap.restype = ctypes.c_ulong
        self.lib.XCreateWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
            ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(SetWindowAttributes)]
        self.lib.XCreateWindow.restype = ctypes.c_ulong
        self.lib.XQueryColor.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Color)]
        self.lib.XStoreColor.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Color)]
        visual = VisualInfo()
        if not self.lib.XMatchVisualInfo(self.d, 0, 24, 5, ctypes.byref(visual)):
            raise RuntimeError("Dedicated Xvfb must expose 24-bit DirectColor")
        colormap = self.lib.XCreateColormap(self.d, parent, visual.visual, 1)  # AllocAll
        attributes = SetWindowAttributes()
        attributes.colormap = colormap
        attributes.background_pixel = visual.red_mask | visual.green_mask | visual.blue_mask
        window = self.lib.XCreateWindow(self.d, parent, x, y, width, height, 0, 24,
            1, visual.visual, (1 << 1) | (1 << 3) | (1 << 13), ctypes.byref(attributes))
        self.lib.XMapWindow(self.d, window)
        self.sync()
        return window, colormap, attributes.background_pixel

    def color(self, colormap, pixel, replacement=None):
        color = Color()
        color.pixel = pixel
        if replacement is not None:
            color.red, color.green, color.blue = replacement
            color.flags = b"\x07"
            self.lib.XStoreColor(self.d, colormap, ctypes.byref(color))
            self.sync()
        self.lib.XQueryColor(self.d, colormap, ctypes.byref(color))
        return color.red, color.green, color.blue

    def raw_pixels(self, window):
        attrs = self.connection._attributes(window)
        image = self.lib.XGetImage(self.d, window, 0, 0, attrs.width, attrs.height,
                                  ctypes.c_ulong(-1).value, MODULE._ZPIXMAP)
        if not image:
            raise RuntimeError("Dedicated Xvfb image query failed")
        try:
            return ctypes.string_at(image.contents.data,
                                    image.contents.bytes_per_line * image.contents.height)
        finally:
            self.lib.XDestroyImage(image)

    def deletes(self):
        count = 0
        while self.lib.XPending(self.d):
            event = MODULE._XEvent()
            self.lib.XNextEvent(self.d, ctypes.byref(event))
            if event.client.type == 33 and event.client.window == self.window:
                count += 1
        return count

    def close(self):
        for window in self.covers:
            self.lib.XDestroyWindow(self.d, window)
        self.lib.XDestroyWindow(self.d, self.frame)
        self.sync()
        self.connection.close()


@unittest.skipUnless(sys.platform.startswith("linux") and XVFB, "requires Linux and Xvfb")
class NativeCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.display = Display()

    @classmethod
    def tearDownClass(cls):
        cls.display.close()

    def setUp(self):
        self.canvas = Canvas(self.display.name)
        self.addCleanup(self.canvas.close)
        self.original_inventory = MODULE._inventory.inspect_dialogs
        self.original_open = MODULE._open_native_connection
        self.addCleanup(self.restore)
        self.target = {"pid": os.getpid(), "display": self.display.name,
                       "ciw_window": "0x%x" % self.canvas.frame}
        self.dialog = {"window_id": "0x%x" % self.canvas.window, "title": "Native reviewed information",
                       "mapped": True, "modal": True, "ownership": "target"}
        # Only inventory classification/routing are replaced. Capture, visibility,
        # properties, stacking, protocol transmission and state all use real Xlib.
        MODULE._inventory.inspect_dialogs = lambda *a, **k: {
            "status": "blocked", "target": self.target, "dialogs": [self.dialog]}
        MODULE._open_native_connection = lambda *a: MODULE._NativeDialogConnection(self.display.name, {})
        self.command = dict(self.target, op="prepare", window_id="0x%x" % self.canvas.window,
                            title=self.dialog["title"], timeout=5)

    def restore(self):
        MODULE._inventory.inspect_dialogs = self.original_inventory
        MODULE._open_native_connection = self.original_open

    def prepared(self):
        result = MODULE.handle_command(self.command)
        self.assertEqual(result["status"], "prepared", result)
        return result["snapshot"]

    def refused(self, command):
        result = MODULE.handle_command(command)
        self.assertEqual(result["status"], "not_started", result)
        self.assertEqual(result["action_sent"], False)
        self.assertEqual(self.canvas.deletes(), 0)
        return result["diagnostic"]

    def test_partially_obscured_prepare(self):
        self.canvas.cover(80)
        self.canvas.redraw()
        self.assertIn("obscured", self.refused(self.command))

    def test_fully_obscured_prepare(self):
        self.canvas.cover()
        self.canvas.redraw()
        self.assertIn("obscured", self.refused(self.command))

    def changed_while_covered(self, width):
        snapshot = self.prepared()
        self.canvas.cover(width)
        self.canvas.redraw()
        self.assertIn("obscured", self.refused({"op": "close", "snapshot": snapshot, "timeout": 5}))

    def test_partially_covered_content_changes_without_approval(self):
        self.changed_while_covered(80)

    def test_fully_covered_content_changes_without_approval(self):
        self.changed_while_covered(160)

    def test_cover_appearing_at_final_grab_is_rechecked(self):
        snapshot = self.prepared()
        def open_connection(*args):
            connection = MODULE._NativeDialogConnection(self.display.name, {})
            grab, calls = connection.grab, [0]
            def raced_grab():
                calls[0] += 1
                if calls[0] == 2:
                    self.canvas.cover(80)
                    self.canvas.redraw()
                grab()
            connection.grab = raced_grab
            return connection
        MODULE._open_native_connection = open_connection
        self.assertIn("obscured", self.refused({"op": "close", "snapshot": snapshot, "timeout": 5}))

    def test_nested_sibling_occlusion(self):
        self.canvas.cover(80, nested=True)
        self.assertIn("obscured", self.refused(self.command))

    def test_border_only_occlusion(self):
        self.canvas.cover(80, border_only=True)
        self.assertIn("obscured", self.refused(self.command))

    def test_ancestor_clips_target(self):
        self.canvas.lib.XMoveWindow(self.canvas.d, self.canvas.window, -15, 25)
        self.canvas.sync()
        self.assertIn("clipped", self.refused(self.command))

    def shape(self, window):
        class Rectangle(ctypes.Structure):
            _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                        ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]
        library = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xext"))
        library.XShapeCombineRectangles.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(Rectangle),
            ctypes.c_int, ctypes.c_int, ctypes.c_int]
        rectangle = Rectangle(0, 0, 80, 60)
        library.XShapeCombineRectangles(self.canvas.d, window, 0, 0, 0,
                                       ctypes.byref(rectangle), 1, 0, 0)
        self.canvas.sync()

    def test_shaped_target_cannot_approve_undefined_pixels(self):
        self.shape(self.canvas.window)
        self.assertIn("shaped", self.refused(self.command))

    def test_shaped_ancestor_cannot_approve_undefined_pixels(self):
        self.shape(self.canvas.frame)
        self.assertIn("shaped", self.refused(self.command))

    def test_directcolor_palette_changes_cannot_approve_unchanged_pixel_indices(self):
        window, colormap, pixel = self.canvas.direct_color(self.canvas.frame, 15, 25)
        self.canvas.window = window
        self.dialog["window_id"] = self.command["window_id"] = "0x%x" % window
        self.canvas.property("_NET_WM_PID", 6, 32, (ctypes.c_ulong * 1)(os.getpid()), 1)
        title = ctypes.create_string_buffer(b"Native reviewed information")
        self.canvas.property("_NET_WM_NAME", self.canvas.connection._atom("UTF8_STRING"),
                             8, title, len(title.value))
        protocols = (ctypes.c_ulong * 1)(self.canvas.connection._atom("WM_DELETE_WINDOW"))
        self.canvas.lib.XSetWMProtocols(self.canvas.d, window, protocols, 1)
        self.canvas.lib.XSetTransientForHint(self.canvas.d, window, self.canvas.frame)
        self.canvas.sync()
        pixels = self.canvas.raw_pixels(window)
        before = self.canvas.color(colormap, pixel)
        after = self.canvas.color(colormap, pixel, tuple(65535 - channel for channel in before))
        self.assertNotEqual(before, after)
        self.assertEqual(pixels, self.canvas.raw_pixels(window))
        self.assertIn("TrueColor", self.refused(self.command))

    def test_same_depth_directcolor_inferior_cannot_approve_pixels(self):
        self.canvas.direct_color(self.canvas.window, 10, 10, 40, 20)
        self.assertIn("visual", self.refused(self.command))

    def test_directcolor_inferior_at_final_grab_is_rechecked(self):
        snapshot = self.prepared()
        def open_connection(*args):
            connection = MODULE._NativeDialogConnection(self.display.name, {})
            grab, calls = connection.grab, [0]
            def raced_grab():
                calls[0] += 1
                if calls[0] == 2:
                    self.canvas.direct_color(self.canvas.window, 10, 10, 40, 20)
                grab()
            connection.grab = raced_grab
            return connection
        MODULE._open_native_connection = open_connection
        self.assertIn("visual", self.refused({"op": "close", "snapshot": snapshot, "timeout": 5}))

    def test_uncovered_target_receives_one_message_but_remains_mapped(self):
        snapshot = self.prepared()
        result = MODULE.handle_command({"op": "close", "snapshot": snapshot, "timeout": 5})
        self.assertEqual(result, {"status": "requested", "action_sent": True})
        self.assertEqual(self.canvas.deletes(), 1)
        self.assert_state(snapshot, True, True)

    def assert_state(self, snapshot, exists, mapped):
        state = MODULE.handle_command({"op": "state", "snapshot": snapshot, "timeout": 5})
        self.assertEqual(state["status"], "window_state", state)
        self.assertEqual(state["target"], self.target)
        self.assertEqual(state["window_id"], "0x%x" % self.canvas.window)
        self.assertIs(state["exists"], exists)
        self.assertIs(state["mapped"], mapped)

    def test_unmapped_exact_window(self):
        snapshot = self.prepared()
        self.canvas.lib.XUnmapWindow(self.canvas.d, self.canvas.window)
        self.canvas.sync()
        self.assert_state(snapshot, True, False)

    def test_destroyed_exact_window(self):
        snapshot = self.prepared()
        self.canvas.lib.XDestroyWindow(self.canvas.d, self.canvas.window)
        self.canvas.sync()
        self.assert_state(snapshot, False, False)

    def test_parent_unmapped_does_not_mean_target_unmapped(self):
        snapshot = self.prepared()
        self.canvas.lib.XUnmapWindow(self.canvas.d, self.canvas.frame)
        self.canvas.sync()
        self.assert_state(snapshot, True, True)

    def dismissal_result(self):
        result = MODULE._inventory.dismiss_window(
            self.display.name, "0x%x" % self.canvas.window, action="enter")
        result["display"] = self.display.name
        return MODULE._inventory._verify_dismissal(result)

    def test_sent_key_to_unresponsive_window_is_only_requested(self):
        result = self.dismissal_result()
        self.assertEqual(result["status"], "requested", result)
        self.assertIs(result["action_sent"], True)
        self.assertIs(result["closure_confirmed"], False)
        self.assertNotIn("dismissed", result)

    def test_dismissal_confirmation_uses_exact_native_mapping(self):
        helper = MODULE._inventory
        target = "0x%x" % self.canvas.window
        self.canvas.lib.XUnmapWindow(self.canvas.d, self.canvas.frame)
        self.canvas.sync()
        result = helper._verify_dismissal({"dismissed": target, "display": self.display.name})
        self.assertEqual(result["status"], "requested", result)
        self.canvas.lib.XUnmapWindow(self.canvas.d, self.canvas.window)
        self.canvas.sync()
        result = helper._verify_dismissal({"dismissed": target, "display": self.display.name})
        self.assertEqual(result["status"], "closed", result)
        self.canvas.lib.XDestroyWindow(self.canvas.d, self.canvas.window)
        self.canvas.sync()
        result = helper._verify_dismissal({"dismissed": target, "display": self.display.name})
        self.assertEqual(result["status"], "closed", result)


if __name__ == "__main__":
    if not sys.platform.startswith("linux") or not XVFB:
        sys.exit("Native coverage requires Linux and Xvfb; refusing a silently skipped CI step")
    unittest.main()

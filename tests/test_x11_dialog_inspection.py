from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest


def _load_helper_module():
    path = (
        Path(__file__).parents[1]
        / "src"
        / "virtuoso_bridge"
        / "resources"
        / "x11_dismiss_dialog.py"
    )
    spec = importlib.util.spec_from_file_location("x11_dialog_inspection_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper():
    return _load_helper_module()


def _record(
    window_id,
    *,
    title="",
    pid=None,
    transient_for=None,
    client_leader=None,
    client_machine=None,
    modal=False,
    geometry=None,
    classes=None,
    mapped=True,
):
    return {
        "id": window_id,
        "frame_id": window_id,
        "title": title,
        "class": classes if classes is not None else ["virtuoso", "Virtuoso"],
        "geometry": geometry if geometry is not None else {"x": 10, "y": 20, "w": 320, "h": 180},
        "mapped": mapped,
        "pid": pid,
        "transient_for": transient_for,
        "client_leader": client_leader,
        "client_machine": client_machine,
        "modal": modal,
    }


def _install_inventory(monkeypatch, helper, inventory, *, display=":7", hostname="eda-local"):
    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: {
            "DISPLAY": display,
            "XAUTHORITY": "/tmp/selected.Xauthority",
            "HOSTNAME": hostname,
        },
    )
    monkeypatch.setattr(
        helper,
        "_inspect_window_inventory",
        lambda selected_display, process_env, timeout=15: list(inventory),
    )


class _FakeX11Inventory:
    def __init__(self, tree, attributes, *, clock=None, cost=0.0):
        self.tree = tree
        self.window_attributes = attributes
        self.clock = clock
        self.cost = cost
        self.calls = []
        self.closed = False

    def _tick(self):
        if self.clock is not None:
            self.clock[0] += self.cost

    def root(self):
        return 1

    def children(self, window_id):
        self.calls.append(("children", window_id))
        self._tick()
        return list(self.tree.get(window_id, []))

    def attributes(self, window_id):
        self.calls.append(("attributes", window_id))
        self._tick()
        return self.window_attributes.get(window_id)

    def close(self):
        self.closed = True


def _xattrs(mapped, x=0, y=0, w=320, h=180, window_class=1,
            depth=24, override_redirect=False):
    return {
        "mapped": mapped,
        "window_class": window_class,
        "depth": depth,
        "override_redirect": override_redirect,
        "geometry": {"x": x, "y": y, "w": w, "h": h},
    }


def _xprop(*, title="", pid=None, transient_for=None, client_leader=None,
           classes=None, modal=False, wm_state=True):
    lines = []
    if pid is not None:
        lines.append("_NET_WM_PID(CARDINAL) = %d" % pid)
    if transient_for is not None:
        lines.append(
            "WM_TRANSIENT_FOR(WINDOW): window id # %s" % transient_for
        )
    if client_leader is not None:
        lines.append(
            "WM_CLIENT_LEADER(WINDOW): window id # %s" % client_leader
        )
    if modal:
        lines.append("_NET_WM_STATE(ATOM) = _NET_WM_STATE_MODAL")
    if title:
        lines.append('_NET_WM_NAME(UTF8_STRING) = "%s"' % title)
    if classes:
        lines.append(
            'WM_CLASS(STRING) = "%s", "%s"' % (classes[0], classes[1])
        )
    if wm_state:
        lines.append("WM_STATE(WM_STATE): window state: Normal")
    return "\n".join(lines)


def test_native_attributes_skip_coordinate_translation_for_unmapped_window(helper):
    class FakeXlib:
        def XGetWindowAttributes(self, _display, _window_id, attributes_pointer):
            attributes = attributes_pointer._obj
            attributes.map_state = 0
            attributes.x = 7
            attributes.y = 9
            attributes.width = 100
            attributes.height = 30
            return 1

        def XTranslateCoordinates(self, *_args):
            pytest.fail("unmapped windows must not trigger coordinate translation")

    connection = object.__new__(helper._X11InventoryConnection)
    connection._xlib = FakeXlib()
    connection._display = 1
    connection._root = 1
    connection._x_error = False

    assert connection.attributes(0x2000) == {
        "mapped": False,
        "window_class": 0,
        "depth": 0,
        "override_redirect": False,
        "geometry": {"x": 7, "y": 9, "w": 100, "h": 30},
    }


def test_native_bad_window_is_an_inspection_failure(helper):
    class FakeXlib:
        def XGetWindowAttributes(self, _display, _window_id, _attributes_pointer):
            connection._handle_x_error(None, None)
            return 0

    connection = object.__new__(helper._X11InventoryConnection)
    connection._xlib = FakeXlib()
    connection._display = 1
    connection._root = 1
    connection._x_error = False

    with pytest.raises(helper._InspectionFailure, match="cannot read X11 window attributes"):
        connection.attributes(0xDEAD)


def test_mapped_input_only_root_is_not_a_visual_dialog_candidate(
    monkeypatch, helper
):
    input_only = 0xC00014
    connection = _FakeX11Inventory(
        {1: [input_only]},
        {
            input_only: _xattrs(
                True,
                w=1920,
                h=1080,
                window_class=2,
                depth=0,
                override_redirect=True,
            )
        },
    )
    monkeypatch.setattr(helper, "_open_x11_inventory", lambda *_args: connection)
    monkeypatch.setattr(
        helper,
        "_bounded_check_output",
        lambda *_args, **_kwargs: pytest.fail("InputOnly root must not run xprop"),
    )

    inventory = helper._inspect_window_inventory(
        ":7", {"DISPLAY": ":7", "XAUTHORITY": None}
    )

    assert inventory == []
    assert connection.closed is True


def test_failed_metadata_on_large_virtuoso_window_is_not_clear(monkeypatch, helper):
    unknown = _record("0x102", title="Unknown Virtuoso window", geometry={"w": 1200, "h": 900})
    unknown["metadata_error"] = "xprop failed"
    _install_inventory(monkeypatch, helper, [
        _record("0x101", title="Virtuoso Command Interpreter Window", pid=1101), unknown,
    ])
    result = helper.inspect_dialogs(1101)
    assert result["status"] == "indeterminate"
    assert result["dialogs"][0]["ownership"] == "unknown"


def test_zero_x11_pid_is_missing_ownership_not_foreign(helper):
    assert helper._parse_xprop_inspection("_NET_WM_PID(CARDINAL) = 0")["pid"] is None


def test_inspection_isolates_two_virtuoso_processes_on_same_display_and_marks_source_unknown(
    monkeypatch, helper
):
    inventory = [
        _record("0x101", title="Virtuoso Command Interpreter Window", pid=1101),
        _record("0x102", title="Save As", transient_for="0x101"),
        _record("0x201", title="Virtuoso Command Interpreter Window", pid=2202),
        _record("0x202", title="ADE Explorer Update and Run", transient_for="0x201"),
    ]
    _install_inventory(monkeypatch, helper, inventory)

    result = helper.inspect_dialogs(1101)

    assert result["status"] == "blocked"
    assert result["target"] == {"pid": 1101, "display": ":7", "ciw_window": "0x101"}
    assert [dialog["window_id"] for dialog in result["dialogs"]] == ["0x102"]
    assert result["dialogs"][0]["source"] == "unknown"
    assert result["dialogs"][0]["ownership"] == "target"
    assert result["dialogs"][0]["suggested_action"] is None
    assert result["diagnostics"] == []


def test_client_leader_chain_establishes_exact_process_ownership(monkeypatch, helper):
    inventory = [
        _record("0x100", title="Virtuoso CIW", pid=77),
        _record("0x110", title="", pid=77, classes=[]),
        _record("0x111", title="", client_leader="0x110", classes=[]),
        _record("0x112", title="Warning", client_leader="0x111", modal=True),
    ]
    _install_inventory(monkeypatch, helper, inventory)

    result = helper.inspect_dialogs(77)

    assert result["status"] == "blocked"
    assert [dialog["window_id"] for dialog in result["dialogs"]] == ["0x112"]


def test_same_pid_from_foreign_x11_client_host_is_not_attributed(monkeypatch, helper):
    inventory = [
        _record(
            "0x100",
            title="Virtuoso CIW",
            pid=77,
            client_machine="eda-local",
        ),
        _record(
            "0x170",
            title="Foreign candidate",
            pid=77,
            client_machine="eda-remote",
        ),
    ]
    _install_inventory(monkeypatch, helper, inventory, hostname="eda-local")

    result = helper.inspect_dialogs(77)

    assert result["status"] == "clear"
    assert result["dialogs"] == []
    assert result["diagnostics"] == []


def test_missing_candidate_ownership_is_indeterminate(monkeypatch, helper):
    inventory = [
        _record("0x100", title="Virtuoso CIW", pid=77),
        _record("0x120", title="Unowned warning"),
    ]
    _install_inventory(monkeypatch, helper, inventory)

    result = helper.inspect_dialogs(77)

    assert result["status"] == "indeterminate"
    assert result["dialogs"][0]["ownership"] == "unknown"
    assert "has no PID or ownership relation" in result["diagnostics"][0]


def test_cyclic_candidate_ownership_is_indeterminate(monkeypatch, helper):
    inventory = [
        _record("0x100", title="Virtuoso CIW", pid=77),
        _record("0x130", title="Cyclic warning", transient_for="0x131"),
        _record("0x131", title="", client_leader="0x130", classes=[]),
    ]
    _install_inventory(monkeypatch, helper, inventory)

    result = helper.inspect_dialogs(77)

    assert result["status"] == "indeterminate"
    assert "cycle" in result["diagnostics"][0]


def test_contradictory_pid_and_leader_metadata_is_indeterminate(monkeypatch, helper):
    inventory = [
        _record("0x100", title="Virtuoso CIW", pid=77),
        _record("0x140", title="Contradictory warning", pid=77, client_leader="0x141"),
        _record("0x141", title="", pid=88, classes=[]),
    ]
    _install_inventory(monkeypatch, helper, inventory)

    result = helper.inspect_dialogs(77)

    assert result["status"] == "indeterminate"
    assert result["dialogs"][0]["ownership"] == "unknown"
    assert "contradictory ownership metadata" in result["diagnostics"][0]


def test_stale_pid_fails_without_x11_probe(monkeypatch, helper):
    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: (_ for _ in ()).throw(
            helper._InspectionFailure("process 404 is unavailable or stale")
        ),
    )
    monkeypatch.setattr(
        helper,
        "_inspect_window_inventory",
        lambda *_args: pytest.fail("stale PID must not trigger an X11 probe"),
    )

    result = helper.inspect_dialogs(404)

    assert result["status"] == "indeterminate"
    assert result["target"] == {"pid": 404, "display": None, "ciw_window": None}
    assert "unavailable or stale" in result["diagnostics"][0]


def test_ambiguous_ciw_for_selected_pid_is_indeterminate(monkeypatch, helper):
    inventory = [
        _record("0x100", title="Virtuoso CIW", pid=77),
        _record("0x101", title="Virtuoso Command Interpreter Window", pid=77),
    ]
    _install_inventory(monkeypatch, helper, inventory)

    result = helper.inspect_dialogs(77)

    assert result["status"] == "indeterminate"
    assert "more than one CIW" in result["diagnostics"][0]


def test_supplied_ciw_must_belong_to_selected_pid(monkeypatch, helper):
    inventory = [
        _record("0x100", title="Virtuoso CIW", pid=77),
        _record("0x200", title="Virtuoso CIW", pid=88),
    ]
    _install_inventory(monkeypatch, helper, inventory)

    result = helper.inspect_dialogs(77, ciw_window="0x200")

    assert result["status"] == "indeterminate"
    assert "belongs to PID 88" in result["diagnostics"][0]


def test_supplied_decimal_ciw_id_is_verified_but_echoed_verbatim(monkeypatch, helper):
    inventory = [_record("0x100", title="Virtuoso CIW", pid=77)]
    _install_inventory(monkeypatch, helper, inventory)

    result = helper.inspect_dialogs(77, ciw_window="256")

    assert result["status"] == "clear"
    assert result["target"]["ciw_window"] == "256"


def test_non_geometry_modal_is_still_blocking(monkeypatch, helper):
    inventory = [
        _record("0x100", title="Virtuoso CIW", pid=77),
        _record(
            "0x150",
            title="Application Modal",
            pid=77,
            modal=True,
            geometry={"x": 0, "y": 0, "w": 1800, "h": 1000},
        ),
    ]
    _install_inventory(monkeypatch, helper, inventory)

    result = helper.inspect_dialogs(77)

    assert result["status"] == "blocked"
    assert result["dialogs"][0]["window_id"] == "0x150"
    assert result["dialogs"][0]["modal"] is True


def test_missing_process_display_and_explicit_display_mismatch_fail_closed(monkeypatch, helper):
    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: {"DISPLAY": None, "XAUTHORITY": None},
    )
    missing = helper.inspect_dialogs(77)
    assert missing["status"] == "indeterminate"
    assert "no DISPLAY" in missing["diagnostics"][0]

    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: {"DISPLAY": ":7", "XAUTHORITY": None},
    )
    monkeypatch.setattr(
        helper,
        "_inspect_window_inventory",
        lambda *_args: pytest.fail("DISPLAY mismatch must not be probed"),
    )
    mismatch = helper.inspect_dialogs(77, display=":8")
    assert mismatch["status"] == "indeterminate"
    assert mismatch["target"]["display"] == ":7"
    assert "disagrees" in mismatch["diagnostics"][0]


def test_many_unmapped_root_children_are_filtered_before_metadata_probes(
    monkeypatch, helper
):
    unmapped = list(range(0x2000, 0x23C0))
    frame_ciw = 0xF00
    frame_dialog = 0xF10
    ciw = 0x100
    dialog = 0x110
    tree = {
        1: unmapped + [frame_ciw, frame_dialog],
        frame_ciw: [ciw],
        frame_dialog: [dialog],
    }
    attributes = {window_id: _xattrs(False, w=100, h=30) for window_id in unmapped}
    attributes.update({
        frame_ciw: _xattrs(True, w=900, h=300),
        frame_dialog: _xattrs(True, w=320, h=180),
        ciw: _xattrs(True, w=900, h=300),
        dialog: _xattrs(True, w=320, h=180),
    })
    connection = _FakeX11Inventory(tree, attributes)
    metadata = {
        "0xf00": _xprop(wm_state=False),
        "0xf10": _xprop(wm_state=False),
        "0x100": _xprop(
            title="Virtuoso CIW", pid=77, classes=("virtuoso", "Virtuoso")
        ),
        "0x110": _xprop(
            title="Warning",
            transient_for="0x100",
            classes=("virtuoso", "Virtuoso"),
            modal=True,
        ),
    }
    commands = []

    monkeypatch.setattr(helper, "_open_x11_inventory", lambda *_args: connection)

    def fake_probe(command, env=None, timeout=None):
        commands.append(command)
        assert command[:2] == ["xprop", "-id"]
        return metadata[command[2]]

    monkeypatch.setattr(helper, "_bounded_check_output", fake_probe)

    inventory = helper._inspect_window_inventory(
        ":7", {"DISPLAY": ":7", "XAUTHORITY": "/tmp/auth"}
    )

    assert len(tree[1]) == 962
    assert [record["id"] for record in inventory] == [
        "0xf00", "0xf10", "0x100", "0x110"
    ]
    assert len(commands) == 4
    assert all(command[0] == "xprop" for command in commands)
    assert connection.closed is True


def test_hidden_ownership_leader_is_resolved_on_demand(monkeypatch, helper):
    frame_ciw = 0xF00
    frame_dialog = 0xF10
    ciw = 0x100
    dialog = 0x110
    hidden_leader = 0x900
    connection = _FakeX11Inventory(
        {
            1: [frame_ciw, frame_dialog],
            frame_ciw: [ciw],
            frame_dialog: [dialog],
        },
        {
            frame_ciw: _xattrs(True),
            frame_dialog: _xattrs(True),
            ciw: _xattrs(True),
            dialog: _xattrs(True),
            hidden_leader: _xattrs(
                True, window_class=2, depth=0, override_redirect=True
            ),
        },
    )
    metadata = {
        "0xf00": _xprop(wm_state=False),
        "0xf10": _xprop(wm_state=False),
        "0x100": _xprop(
            title="Virtuoso CIW", pid=77, classes=("virtuoso", "Virtuoso")
        ),
        "0x110": _xprop(
            title="Warning",
            client_leader="0x900",
            classes=("virtuoso", "Virtuoso"),
            modal=True,
        ),
        "0x900": _xprop(pid=77, wm_state=False),
    }
    monkeypatch.setattr(helper, "_open_x11_inventory", lambda *_args: connection)
    monkeypatch.setattr(
        helper,
        "_bounded_check_output",
        lambda command, env=None, timeout=None: metadata[command[2]],
    )
    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: {"DISPLAY": ":7", "XAUTHORITY": None, "HOSTNAME": "eda-local"},
    )

    result = helper.inspect_dialogs(77)

    assert result["status"] == "blocked"
    assert [dialog["window_id"] for dialog in result["dialogs"]] == ["0x110"]
    assert ("attributes", hidden_leader) in connection.calls
    assert connection.closed is True


def test_mapped_shell_metadata_failure_is_indeterminate_and_closes_xlib(
    monkeypatch, helper
):
    frame_ciw = 0xF00
    frame_unknown = 0xF10
    ciw = 0x100
    unknown = 0x110
    connection = _FakeX11Inventory(
        {1: [frame_ciw, frame_unknown], frame_ciw: [ciw], frame_unknown: [unknown]},
        {
            frame_ciw: _xattrs(True),
            frame_unknown: _xattrs(True),
            ciw: _xattrs(True),
            unknown: _xattrs(True),
        },
    )
    monkeypatch.setattr(helper, "_open_x11_inventory", lambda *_args: connection)
    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: {"DISPLAY": ":7", "XAUTHORITY": None, "HOSTNAME": "eda-local"},
    )

    def fake_probe(command, env=None, timeout=None):
        window_id = command[2]
        if window_id == "0x100":
            return _xprop(
                title="Virtuoso CIW", pid=77, classes=("virtuoso", "Virtuoso")
            )
        if window_id == "0x110":
            raise helper._InspectionFailure("xprop metadata failed")
        return _xprop(wm_state=False)

    monkeypatch.setattr(helper, "_bounded_check_output", fake_probe)

    result = helper.inspect_dialogs(77)

    assert result["status"] == "indeterminate"
    assert any(
        "xprop metadata failed" in diagnostic
        for diagnostic in result["diagnostics"]
    )
    assert connection.closed is True


def test_mapped_input_output_without_shell_metadata_is_not_false_clear(
    monkeypatch, helper
):
    frame_ciw = 0xF00
    ciw = 0x100
    unknown = 0xC00009
    connection = _FakeX11Inventory(
        {1: [frame_ciw, unknown], frame_ciw: [ciw], unknown: []},
        {
            frame_ciw: _xattrs(True),
            ciw: _xattrs(True),
            unknown: _xattrs(
                True,
                x=-100,
                y=-100,
                w=1,
                h=1,
                window_class=1,
                depth=24,
                override_redirect=True,
            ),
        },
    )
    metadata = {
        "0xf00": _xprop(wm_state=False),
        "0x100": _xprop(
            title="Virtuoso CIW", pid=77, classes=("virtuoso", "Virtuoso")
        ),
        "0xc00009": _xprop(wm_state=False),
    }
    monkeypatch.setattr(helper, "_open_x11_inventory", lambda *_args: connection)
    monkeypatch.setattr(
        helper,
        "_bounded_check_output",
        lambda command, env=None, timeout=None: metadata[command[2]],
    )
    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: {"DISPLAY": ":7", "XAUTHORITY": None, "HOSTNAME": "eda-local"},
    )

    result = helper.inspect_dialogs(77)

    assert result["status"] == "indeterminate"
    assert [dialog["window_id"] for dialog in result["dialogs"]] == ["0xc00009"]
    assert "no inspectable client-shell metadata" in result["diagnostics"][0]


def test_inventory_aggregate_timeout_is_indeterminate(monkeypatch, helper):
    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: {"DISPLAY": ":7", "XAUTHORITY": None, "HOSTNAME": "eda-local"},
    )
    now = [0.0]
    monkeypatch.setattr(helper.time, "monotonic", lambda: now[0])
    connection = _FakeX11Inventory(
        {1: [0xF00]},
        {0xF00: _xattrs(True)},
        clock=now,
        cost=1.5,
    )
    monkeypatch.setattr(helper, "_open_x11_inventory", lambda *_args: connection)
    monkeypatch.setattr(
        helper,
        "_bounded_check_output",
        lambda *_args, **_kwargs: pytest.fail("budget must expire before xprop"),
    )

    result = helper.inspect_dialogs(77, timeout=3)

    assert result["status"] == "indeterminate"
    assert result["diagnostics"] == ["X11 inspection budget exhausted"]
    assert connection.closed is True


@pytest.mark.parametrize("case,expected", [
    ("sentinel", "clear"),
    ("visible", "indeterminate"),
    ("normal_window", "indeterminate"),
    ("larger", "indeterminate"),
    ("missing_root", "indeterminate"),
    ("property_error", "indeterminate"),
    ("window_type", "indeterminate"),
    ("known_modal", "blocked"),
])
def test_offscreen_sentinel_filter_is_narrow(monkeypatch, helper, case, expected):
    ciw = 0x100
    sentinel = 0x200
    attrs = _xattrs(True, x=-100, y=-100, w=1, h=1, override_redirect=True)
    if case == "visible":
        attrs["geometry"]["x"] = attrs["geometry"]["y"] = 0
    elif case == "normal_window":
        attrs["override_redirect"] = False
    elif case == "larger":
        attrs["geometry"]["w"] = 2
    attributes = {ciw: _xattrs(True), sentinel: attrs}
    if case != "missing_root":
        attributes[1] = _xattrs(True, w=1920, h=1080)
    connection = _FakeX11Inventory({1: [ciw, sentinel]}, attributes)
    monkeypatch.setattr(helper, "_open_x11_inventory", lambda *_args: connection)
    monkeypatch.setattr(helper, "_read_process_x11_env", lambda _pid: {
        "DISPLAY": ":7", "XAUTHORITY": None, "HOSTNAME": "eda-local",
    })

    def probe(command, **kwargs):
        if command[2] == "0x100":
            return _xprop(title="Virtuoso CIW", pid=77, classes=("virtuoso", "Virtuoso"))
        if case == "property_error":
            raise helper._InspectionFailure("xprop failed")
        if case == "window_type":
            return "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DIALOG"
        if case == "known_modal":
            return _xprop(title="Question", pid=77, modal=True,
                          classes=("virtuoso", "Virtuoso"), wm_state=False)
        return _xprop(wm_state=False)

    monkeypatch.setattr(helper, "_bounded_check_output", probe)
    report = helper.inspect_dialogs(77)
    assert report["status"] == expected
    assert connection.closed
    if expected == "clear":
        assert not report["dialogs"]


def test_boolean_pid_is_rejected_before_process_probe(monkeypatch, helper):
    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: pytest.fail("boolean PID must be rejected before /proc access"),
    )

    result = helper.inspect_dialogs(True)

    assert result["status"] == "indeterminate"
    assert result["target"]["pid"] == 0
    assert "positive integer" in result["diagnostics"][0]


def test_process_metadata_comes_only_from_selected_proc_pid(monkeypatch, helper):
    opened = []

    def fake_open(path, mode="r"):
        opened.append(path)
        if path == "/proc/321/stat":
            return io.BytesIO(b"321 (virtuoso) S 1 2 3")
        if path == "/proc/321/cmdline":
            return io.BytesIO(b"/cad/bin/virtuoso\x00-replay\x00session.il\x00")
        if path == "/proc/321/environ":
            return io.BytesIO(
                b"DISPLAY=:42\x00XAUTHORITY=/run/user/321/gdm/Xauthority\x00HOSTNAME=eda-local\x00"
            )
        raise AssertionError("unexpected path: %s" % path)

    monkeypatch.setattr(helper, "open", fake_open, raising=False)
    monkeypatch.setattr(
        helper.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("process selection must not scan with subprocesses"),
    )

    result = helper._read_process_x11_env(321)

    assert result == {
        "DISPLAY": ":42",
        "XAUTHORITY": "/run/user/321/gdm/Xauthority",
        "HOSTNAME": "eda-local",
    }
    assert opened == ["/proc/321/stat", "/proc/321/cmdline", "/proc/321/environ"]


def test_wrong_process_is_rejected_from_selected_proc_metadata(monkeypatch, helper):
    def fake_open(path, mode="r"):
        if path == "/proc/654/stat":
            return io.BytesIO(b"654 (python) S 1 2 3")
        if path == "/proc/654/cmdline":
            return io.BytesIO(b"/usr/bin/python\x00worker.py\x00")
        if path == "/proc/654/environ":
            return io.BytesIO(b"DISPLAY=:7\x00HOSTNAME=eda-local\x00")
        raise AssertionError("unexpected path: %s" % path)

    monkeypatch.setattr(helper, "open", fake_open, raising=False)

    with pytest.raises(helper._InspectionFailure, match="not a live interactive Virtuoso"):
        helper._read_process_x11_env(654)


def test_xprop_parser_reads_pid_relations_modal_state_and_client_host(helper):
    parsed = helper._parse_xprop_inspection(
        '\n'.join([
            '_NET_WM_PID(CARDINAL) = 77',
            'WM_TRANSIENT_FOR(WINDOW): window id # 0x100',
            'WM_CLIENT_LEADER(WINDOW): window id # 0x101',
            'WM_CLIENT_MACHINE(STRING) = "eda-remote.example"',
            '_NET_WM_STATE(ATOM) = _NET_WM_STATE_ABOVE, _NET_WM_STATE_MODAL',
            '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DIALOG, _NET_WM_WINDOW_TYPE_NORMAL',
            '_NET_WM_NAME(UTF8_STRING) = "Warning"',
            'WM_CLASS(STRING) = "virtuoso", "Virtuoso"',
        ])
    )

    assert parsed == {
        "pid": 77,
        "transient_for": "0x100",
        "client_leader": "0x101",
        "client_machine": "eda-remote.example",
        "modal": True,
        "window_types": ["_NET_WM_WINDOW_TYPE_DIALOG", "_NET_WM_WINDOW_TYPE_NORMAL"],
        "title": "Warning",
        "class": ["virtuoso", "Virtuoso"],
    }


def test_inspect_cli_prints_one_json_object_and_never_injects_enter(monkeypatch, helper, capsys):
    inventory = [
        _record("0x100", title="Virtuoso CIW", pid=77),
        _record("0x160", title="Candidate", pid=77),
    ]
    _install_inventory(monkeypatch, helper, inventory)
    monkeypatch.setattr(
        helper,
        "dismiss_window",
        lambda *_args, **_kwargs: pytest.fail("inspection must not dismiss windows"),
    )
    monkeypatch.setattr(
        helper,
        "_send_enter",
        lambda *_args, **_kwargs: pytest.fail("inspection must not inject Enter"),
    )
    monkeypatch.setattr(
        helper,
        "_type_ascii_into_window",
        lambda *_args, **_kwargs: pytest.fail("inspection must not type into a CIW"),
    )
    monkeypatch.setattr(
        helper.sys,
        "argv",
        ["x11_dismiss_dialog.py", "--inspect-dialogs", "--pid", "77", ":7"],
    )

    with pytest.raises(SystemExit) as exc:
        helper.main()

    assert exc.value.code == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "blocked"
    assert payload["dialogs"][0]["suggested_action"] is None
    assert payload["dialogs"][0]["source"] == "unknown"


def test_inspect_cli_forwards_total_timeout(monkeypatch, helper, capsys):
    captured = []
    monkeypatch.setattr(
        helper,
        "_read_process_x11_env",
        lambda _pid: {"DISPLAY": ":7", "XAUTHORITY": None, "HOSTNAME": "eda-local"},
    )
    monkeypatch.setattr(
        helper,
        "_inspect_window_inventory",
        lambda display, process_env, timeout=15: captured.append(timeout)
        or [_record("0x100", title="Virtuoso CIW", pid=77)],
    )
    monkeypatch.setattr(
        helper.sys,
        "argv",
        [
            "x11_dismiss_dialog.py",
            "--timeout",
            "4.25",
            "--inspect-dialogs",
            "--pid",
            "77",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        helper.main()

    assert exc.value.code == 0
    assert captured == [4.25]
    assert json.loads(capsys.readouterr().out)["status"] == "clear"


def test_inspect_cli_rejects_non_positive_pid_with_one_json_object(monkeypatch, helper, capsys):
    monkeypatch.setattr(
        helper.sys,
        "argv",
        ["x11_dismiss_dialog.py", "--inspect-dialogs", "--pid", "0"],
    )

    with pytest.raises(SystemExit) as exc:
        helper.main()

    assert exc.value.code == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "indeterminate"
    assert payload["target"]["pid"] == 0
    assert "positive integer" in payload["diagnostics"][0]

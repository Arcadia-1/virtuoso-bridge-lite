from __future__ import annotations

from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.maestro import lifecycle
from virtuoso_bridge.virtuoso.maestro.reader.state import (
    MaestroSessionState,
    MaestroStateProbeError,
    get_session_state,
    list_session_states,
    parse_maestro_title,
)
class _Client:
    def __init__(self, *outputs: str, errors: list[str] | None = None) -> None:
        self.outputs = list(outputs)
        self.errors = errors or []
        self.calls: list[tuple[str, dict]] = []
        self.ssh_runner = None

    def execute_skill(self, expression: str, **kwargs):
        self.calls.append((expression, kwargs))
        output = self.outputs.pop(0) if self.outputs else "nil"
        return SimpleNamespace(ok=not self.errors, errors=self.errors, output=output)


@pytest.mark.parametrize(
    ("title", "application", "access", "unsaved"),
    [
        ("ADE Assembler Editing: LIB CELL maestro", "assembler", "editing", False),
        ("ADE Assembler Editing: LIB CELL maestro*", "assembler", "editing", True),
        ("ADE Explorer Reading: LIB CELL maestro", "explorer", "reading", False),
        (
            "ADE Assembler Editing: LIB CELL maestro* Version: 7 -CheckedOut",
            "assembler", "editing", True,
        ),
    ],
)
def test_parse_maestro_title_known_shapes(
    title: str, application: str, access: str, unsaved: bool,
) -> None:
    parsed = parse_maestro_title(title)

    assert parsed == {
        "application": application,
        "access": access,
        "unsaved": unsaved,
        "lib": "LIB",
        "cell": "CELL",
        "view": "maestro",
    }


def test_parse_maestro_title_does_not_guess_unknown_mode() -> None:
    assert parse_maestro_title("ADE Assembler Reviewing: LIB CELL maestro") is None
    assert parse_maestro_title("Virtuoso Schematic Editing: LIB CELL schematic") is None


def test_list_session_states_distinguishes_gui_and_headless() -> None:
    client = _Client(
        '("VB_MAESTRO_STATE_V1" '
        '((t 4 "ADE Assembler Editing: LIB CELL maestro* Version: 7 -CheckedOut" '
        '"fnxSession4" "fnxSession4")) '
        '("fnxSession4" "fnxSession9") 4 '
        '"ADE Assembler Editing: LIB CELL maestro* Version: 7 -CheckedOut")'
    )

    states = list_session_states(client)

    assert len(states) == 2
    assert states[0].model_dump() == {
        "context": "gui",
        "access": "editing",
        "unsaved": True,
        "session": "fnxSession4",
        "window_num": 4,
        "application": "assembler",
        "lib": "LIB",
        "cell": "CELL",
        "view": "maestro",
        "title": "ADE Assembler Editing: LIB CELL maestro* Version: 7 -CheckedOut",
        "current": True,
        "source": "window_title",
        "diagnostics": (),
    }
    assert states[1].context == "headless"
    assert states[1].access == "unknown"
    assert states[1].unsaved is None
    assert states[1].session == "fnxSession9"


def test_get_session_state_reports_non_maestro_current_window() -> None:
    client = _Client(
        '("VB_MAESTRO_STATE_V1" '
        '((t 2 "Virtuoso Schematic Editing: LIB CELL schematic" nil nil)) '
        'nil 2 "Virtuoso Schematic Editing: LIB CELL schematic")'
    )

    state = get_session_state(client)

    assert state.context == "non_maestro_window"
    assert state.window_num == 2
    assert state.unsaved is None


def test_get_session_state_reports_no_current_window() -> None:
    client = _Client('("VB_MAESTRO_STATE_V1" nil nil nil nil)')

    state = get_session_state(client)

    assert state.context == "no_window"
    assert state.unsaved is None


def test_window_session_disagreement_is_explicitly_unknown() -> None:
    client = _Client(
        '("VB_MAESTRO_STATE_V1" '
        '((t 4 "ADE Assembler Editing: LIB CELL maestro" '
        '"fnxSession4" "fnxSession5")) '
        '("fnxSession4" "fnxSession5") 4 '
        '"ADE Assembler Editing: LIB CELL maestro")'
    )

    states = list_session_states(client)

    assert {state.session for state in states} == {"fnxSession4", "fnxSession5"}
    assert all(state.context == "unknown" for state in states)
    assert all(state.unsaved is None for state in states)
    assert all("disagree" in state.diagnostics[0] for state in states)


def test_window_session_disagreement_is_not_reclassified_as_headless() -> None:
    payload = (
        '("VB_MAESTRO_STATE_V1" '
        '((t 4 "ADE Assembler Editing: LIB CELL maestro" '
        '"fnxSession4" "fnxSession5")) '
        '("fnxSession4" "fnxSession5") 4 '
        '"ADE Assembler Editing: LIB CELL maestro")'
    )

    exact = get_session_state(_Client(payload), "fnxSession4")
    current = get_session_state(_Client(payload))

    assert exact.context == "unknown"
    assert exact.session == "fnxSession4"
    assert current.context == "unknown"
    assert "disagree" in current.diagnostics[0]


def test_lifecycle_keeps_ambiguous_window_and_blocks_open() -> None:
    client = _Client(
        '("VB_MAESTRO_STATE_V1" '
        '((t 4 "ADE Assembler Editing: LIB CELL maestro" '
        '"fnxSession4" "fnxSession5")) '
        '("fnxSession4" "fnxSession5") 4 '
        '"ADE Assembler Editing: LIB CELL maestro")'
    )

    windows = lifecycle._get_session_windows(client)

    assert len(windows) == 2
    assert all(window["mode"] == "unknown" for window in windows)
    assert all(window["modified"] is None for window in windows)


def test_unknown_gui_title_preserves_session_without_claiming_clean() -> None:
    client = _Client(
        '("VB_MAESTRO_STATE_V1" '
        '((t 4 "ADE Assembler Reviewing: LIB CELL maestro" '
        '"fnxSession4" "fnxSession4")) '
        '("fnxSession4") 4 "ADE Assembler Reviewing: LIB CELL maestro")'
    )

    state = get_session_state(client, "fnxSession4")

    assert state.context == "gui"
    assert state.access == "unknown"
    assert state.unsaved is None
    assert state.source == "window_inventory"


def test_missing_requested_session_is_not_found() -> None:
    client = _Client('("VB_MAESTRO_STATE_V1" nil ("fnxSession2") nil nil)')

    state = get_session_state(client, "fnxSession8")

    assert state.context == "not_found"
    assert state.session == "fnxSession8"


@pytest.mark.parametrize(
    "payload",
    [
        "nil",
        '("WRONG_TAG" nil nil nil nil)',
        '("VB_MAESTRO_STATE_V1" ((t bad nil nil nil)) nil nil nil)',
        '("VB_MAESTRO_STATE_V1" nil ("fnxSession1" "fnxSession1") nil nil)',
    ],
)
def test_malformed_probe_payload_raises(payload: str) -> None:
    with pytest.raises(MaestroStateProbeError):
        list_session_states(_Client(payload))


def test_probe_execution_failure_is_not_no_window() -> None:
    with pytest.raises(MaestroStateProbeError, match="probe failed"):
        get_session_state(_Client("nil", errors=["CIW unavailable"]))


def _window(
    *, session: str = "fnxSession4", lib: str | None = "LIB",
    cell: str | None = "CELL", mode: str = "editing",
    modified: bool | None = False,
) -> dict:
    return {
        "session": session,
        "window_num": 4,
        "mode": mode,
        "modified": modified,
        "ade_type": "assembler",
        "lib": lib,
        "cell": cell,
        "view": "maestro" if lib else None,
        "title": "title",
        "diagnostics": (),
    }


def test_find_session_for_cell_uses_exact_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "_get_session_windows",
        lambda client: [
            _window(session="fnxSession2", cell="amp2"),
            _window(session="fnxSession1", cell="amp"),
        ],
    )

    assert lifecycle._find_session_for_cell(object(), "LIB", "amp") == "fnxSession1"


def test_find_session_for_cell_rejects_ambiguity(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "_get_session_windows",
        lambda client: [_window(), _window(session="fnxSession5")],
    )

    with pytest.raises(RuntimeError, match="More than one"):
        lifecycle._find_session_for_cell(object(), "LIB", "CELL")


def test_open_gui_session_blocks_unknown_existing_window(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(lifecycle, "_close_background_sessions", lambda client: [])
    monkeypatch.setattr(
        lifecycle, "_get_session_windows",
        lambda client: [_window(mode="unknown", modified=None)],
    )

    with pytest.raises(RuntimeError, match="state is unknown"):
        lifecycle.open_gui_session(client, "LIB", "CELL")

    assert client.calls == []


def test_open_gui_session_does_not_discard_read_only_dirty_window(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(lifecycle, "_close_background_sessions", lambda client: [])
    monkeypatch.setattr(
        lifecycle, "_get_session_windows",
        lambda client: [_window(mode="reading", modified=True)],
    )

    with pytest.raises(RuntimeError, match="read-only"):
        lifecycle.open_gui_session(client, "LIB", "OTHER_CELL")

    assert client.calls == []


def test_open_gui_session_blocks_when_background_session_cannot_close(monkeypatch) -> None:
    client = _Client(errors=["close failed"])
    monkeypatch.setattr(
        lifecycle,
        "list_session_states",
        lambda client: [MaestroSessionState(
            context="headless", session="fnxSession9", source="session_inventory",
        )],
    )

    with pytest.raises(RuntimeError, match="Could not close headless session"):
        lifecycle.open_gui_session(client, "LIB", "CELL")

    assert len(client.calls) == 1


def test_close_gui_session_blocks_unknown_state(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(
        lifecycle, "_get_session_windows",
        lambda client: [_window(mode="unknown", modified=None)],
    )

    with pytest.raises(RuntimeError, match="state is unknown"):
        lifecycle.close_gui_session(client, "fnxSession4")

    assert client.calls == []


def test_close_gui_session_does_not_promote_read_only_dirty_window(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(
        lifecycle, "_get_session_windows",
        lambda client: [_window(mode="reading", modified=True)],
    )

    with pytest.raises(RuntimeError, match="read-only"):
        lifecycle.close_gui_session(client, "fnxSession4", save=True)

    assert client.calls == []


def test_close_headless_session_verifies_disappearance(monkeypatch) -> None:
    client = _Client("t")
    monkeypatch.setattr(lifecycle, "_get_session_windows", lambda client: [])
    states = iter([
        MaestroSessionState(
            context="headless", session="fnxSession4", source="session_inventory",
        ),
        MaestroSessionState(
            context="not_found", session="fnxSession4", source="session_inventory",
        ),
    ])
    monkeypatch.setattr(lifecycle, "get_session_state", lambda *args: next(states))

    lifecycle.close_gui_session(client, "fnxSession4")

    assert 'maeCloseSession(?session "fnxSession4" ?forceClose t)' in client.calls[0][0]


def test_close_gui_session_verifies_window_closed_before_purge(monkeypatch) -> None:
    client = _Client("t")
    monkeypatch.setattr(lifecycle, "_get_session_windows", lambda client: [_window()])
    monkeypatch.setattr(
        lifecycle,
        "get_session_state",
        lambda *args: MaestroSessionState(
            context="gui", session="fnxSession4", source="window_inventory",
        ),
    )
    purged: list[bool] = []
    monkeypatch.setattr(
        lifecycle, "_purge_maestro_cellview", lambda *args, **kwargs: purged.append(True),
    )

    with pytest.raises(RuntimeError, match="still has context gui"):
        lifecycle.close_gui_session(client, "fnxSession4")

    assert purged == []


def test_close_gui_session_purges_only_confirmed_closed_target(monkeypatch) -> None:
    client = _Client("t")
    monkeypatch.setattr(lifecycle, "_get_session_windows", lambda client: [_window()])
    monkeypatch.setattr(
        lifecycle,
        "get_session_state",
        lambda *args: MaestroSessionState(
            context="not_found", session="fnxSession4", source="session_inventory",
        ),
    )
    purged: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        lifecycle,
        "_purge_maestro_cellview",
        lambda client, lib, cell, view, **kwargs: purged.append((lib, cell, view)),
    )

    lifecycle.close_gui_session(client, "fnxSession4")

    assert purged == [("LIB", "CELL", "maestro")]


def test_close_gui_window_uses_gui_runner_for_save_dialog(monkeypatch) -> None:
    client = _Client("t")
    daemon_runner = object()
    gui_runner = object()
    client.ssh_runner = daemon_runner
    client.gui_runner = gui_runner
    client.dialogs = SimpleNamespace(connected_pid=lambda timeout=5: 4242)
    sent: list[tuple[object, int | None]] = []
    monkeypatch.setattr(
        lifecycle,
        "_send_x11_alt_n",
        lambda runner, *, pid=None: sent.append((runner, pid)),
    )
    monkeypatch.setattr(
        lifecycle,
        "get_session_state",
        lambda *args: MaestroSessionState(
            context="not_found", session="fnxSession4", source="session_inventory",
        ),
    )

    lifecycle._close_gui_window(client, _window(modified=True), timeout=1)

    assert sent == [(gui_runner, 4242)]


def test_close_gui_window_skips_blind_alt_n_without_verified_pid(monkeypatch) -> None:
    client = _Client("t")
    client.gui_runner = object()
    sent: list[object] = []
    monkeypatch.setattr(
        lifecycle,
        "_send_x11_alt_n",
        lambda runner, *, pid=None: sent.append((runner, pid)),
    )
    monkeypatch.setattr(
        lifecycle,
        "get_session_state",
        lambda *args: MaestroSessionState(
            context="not_found", session="fnxSession4", source="session_inventory",
        ),
    )

    lifecycle._close_gui_window(client, _window(modified=True), timeout=1)

    assert sent == []


def test_detect_virtuoso_display_never_falls_back_to_pgrep(monkeypatch) -> None:
    monkeypatch.delenv("VB_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    commands: list[str] = []

    class _Runner:
        def run_command(self, cmd: str, timeout: int = 5):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout="DISPLAY=:42\n", stderr="")

    runner = _Runner()
    assert lifecycle._detect_virtuoso_display(runner) == ""
    assert lifecycle._detect_virtuoso_display(None) == ""
    assert commands == []

    assert lifecycle._detect_virtuoso_display(runner, pid=12345) == ":42"
    assert len(commands) == 1
    assert "/proc/12345/environ" in commands[0]
    assert "pgrep" not in commands[0]


def test_send_x11_alt_n_uses_lp64_ctypes_detected_python_and_quoted_display(monkeypatch) -> None:
    import ast
    import shlex

    monkeypatch.setenv("VB_DISPLAY", ":7; touch /tmp/pwn")
    commands: list[str] = []

    class _Runner:
        def run_command(self, cmd: str, timeout: int = 5):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lifecycle, "_detect_remote_python", lambda runner, timeout=5: "python3")

    lifecycle._send_x11_alt_n(_Runner(), pid=4242)

    assert len(commands) == 1
    cmd = commands[0]
    assert "python2.7" not in cmd
    assert "pgrep" not in cmd
    assert cmd.startswith(f"DISPLAY={shlex.quote(':7; touch /tmp/pwn')} python3 -c ")
    parts = shlex.split(cmd)
    assert parts[0] == "DISPLAY=:7; touch /tmp/pwn"
    assert parts[1] == "python3"
    assert parts[2] == "-c"
    script = parts[3]
    ast.parse(script)
    assert "xlib.XOpenDisplay.argtypes=[ctypes.c_char_p]" in script
    assert "xlib.XOpenDisplay.restype=ctypes.c_void_p" in script
    assert "xlib.XCloseDisplay.argtypes=[ctypes.c_void_p]" in script
    assert "xlib.XFlush.argtypes=[ctypes.c_void_p]" in script
    assert "xlib.XKeysymToKeycode.argtypes=[ctypes.c_void_p,ctypes.c_ulong]" in script
    assert "xlib.XKeysymToKeycode.restype=ctypes.c_uint" in script
    assert (
        "xtst.XTestFakeKeyEvent.argtypes="
        "[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_ulong]"
    ) in script
    assert "xtst.XTestFakeKeyEvent.restype=ctypes.c_int" in script

    # Execute the generated script in-process against a stubbed ctypes layer
    # to verify runtime LP64 signature wiring and key event ordering.
    import ctypes as _real_ctypes

    recorded: list[tuple] = []

    class _Fn:
        def __init__(self, name: str, ret) -> None:
            self.name = name
            self.ret = ret
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            recorded.append((self.name, args, self.argtypes, self.restype))
            return self.ret(*args) if callable(self.ret) else self.ret

    fake_xlib = SimpleNamespace(
        XOpenDisplay=_Fn("XOpenDisplay", 0x7FFF12345678),
        XCloseDisplay=_Fn("XCloseDisplay", 0),
        XFlush=_Fn("XFlush", 0),
        XKeysymToKeycode=_Fn("XKeysymToKeycode", lambda d, ks: 64 if ks == 0xFFE9 else 57),
    )
    fake_xtst = SimpleNamespace(
        XTestFakeKeyEvent=_Fn("XTestFakeKeyEvent", 1),
    )
    fake_ctypes = SimpleNamespace(
        c_char_p=_real_ctypes.c_char_p,
        c_void_p=_real_ctypes.c_void_p,
        c_int=_real_ctypes.c_int,
        c_uint=_real_ctypes.c_uint,
        c_ulong=_real_ctypes.c_ulong,
        util=SimpleNamespace(find_library=lambda name: f"lib{name}.so"),
        cdll=SimpleNamespace(
            LoadLibrary=lambda path: fake_xlib if "X11" in path else fake_xtst
        ),
    )
    import builtins
    real_import = builtins.__import__

    def _stub_import(name, *args, **kwargs):
        if name == "ctypes" or name == "ctypes.util":
            return fake_ctypes
        return real_import(name, *args, **kwargs)

    exec(script, {"__builtins__": {**builtins.__dict__, "__import__": _stub_import}})

    assert fake_xlib.XOpenDisplay.restype is _real_ctypes.c_void_p
    assert fake_xlib.XCloseDisplay.argtypes == [_real_ctypes.c_void_p]
    key_events = [entry[1] for entry in recorded if entry[0] == "XTestFakeKeyEvent"]
    assert key_events == [
        (0x7FFF12345678, 64, 1, 0),
        (0x7FFF12345678, 57, 1, 0),
        (0x7FFF12345678, 57, 0, 0),
        (0x7FFF12345678, 64, 0, 0),
    ]
    assert any(entry[0] == "XFlush" and entry[1] == (0x7FFF12345678,) for entry in recorded)
    assert any(entry[0] == "XCloseDisplay" and entry[1] == (0x7FFF12345678,) for entry in recorded)


def test_lifecycle_escapes_skill_strings_across_session_operations(monkeypatch) -> None:
    # 1. _close_background_sessions
    client_bg = _Client("t")
    monkeypatch.setattr(
        lifecycle,
        "list_session_states",
        lambda client: [MaestroSessionState(
            context="headless", session='s"\\bg', source="session_inventory",
        )],
    )
    monkeypatch.setattr(
        lifecycle,
        "get_session_state",
        lambda client, s: MaestroSessionState(
            context="not_found", session=s, source="session_inventory",
        ),
    )
    assert lifecycle._close_background_sessions(client_bg) == ['s"\\bg']
    assert 'maeCloseSession(?session "s\\"\\\\bg" ?forceClose t)' in client_bg.calls[0][0]

    # 2. open_session & close_session
    client_open = _Client('"fnxSession1"', "t")
    assert lifecycle.open_session(client_open, 'lib"\\1', 'cell"\\2') == "fnxSession1"
    assert 'maeOpenSetup("lib\\"\\\\1" "cell\\"\\\\2" "maestro")' in client_open.calls[0][0]
    lifecycle.close_session(client_open, 'sess"\\3')
    assert 'maeCloseSession(?session "sess\\"\\\\3" ?forceClose t)' in client_open.calls[1][0]

    # 3. open_gui_session deOpenCellView
    client_gui = _Client("window:5")
    monkeypatch.setattr(lifecycle, "_close_background_sessions", lambda client: [])
    monkeypatch.setattr(lifecycle, "_get_session_windows", lambda client: [])
    monkeypatch.setattr(lifecycle, "_find_session_for_cell", lambda client, l, c: "fnxSession5")
    assert lifecycle.open_gui_session(client_gui, 'lib"\\a', 'cell"\\b') == "fnxSession5"
    assert 'deOpenCellView("lib\\"\\\\a" "cell\\"\\\\b" "maestro" "maestro" nil "a")' in client_gui.calls[0][0]

    # 4. close_gui_session headless and save branches
    client_hl = _Client("t")
    monkeypatch.setattr(lifecycle, "_get_session_windows", lambda client: [])
    states = iter([
        MaestroSessionState(context="headless", session='hl"\\s', source="session_inventory"),
        MaestroSessionState(context="not_found", session='hl"\\s', source="session_inventory"),
    ])
    monkeypatch.setattr(lifecycle, "get_session_state", lambda *args: next(states))
    lifecycle.close_gui_session(client_hl, 'hl"\\s')
    assert 'maeCloseSession(?session "hl\\"\\\\s" ?forceClose t)' in client_hl.calls[0][0]

    client_save = _Client("t", "t", "t")
    win_sequence = iter([
        [_window(session='sv"\\s', mode="editing", modified=True)],
        [_window(session='sv"\\s', mode="editing", modified=False)],
    ])
    monkeypatch.setattr(lifecycle, "_get_session_windows", lambda client: next(win_sequence))
    close_states = iter([
        MaestroSessionState(context="headless", session='sv"\\s', source="session_inventory"),
        MaestroSessionState(context="not_found", session='sv"\\s', source="session_inventory"),
    ])
    monkeypatch.setattr(lifecycle, "get_session_state", lambda *args: next(close_states))
    monkeypatch.setattr(lifecycle, "_purge_maestro_cellview", lambda *args, **kwargs: None)
    lifecycle.close_gui_session(client_save, 'sv"\\s', save=True)
    assert 'maeSaveSetup(?session "sv\\"\\\\s")' in client_save.calls[0][0]
    assert 'maeCloseSession(?session "sv\\"\\\\s" ?forceClose t)' in client_save.calls[2][0]


def test_close_session_surfaces_skill_errors() -> None:
    client = _Client("nil", errors=["maeCloseSession failed: locked"])
    with pytest.raises(RuntimeError, match="maeCloseSession failed"):
        lifecycle.close_session(client, "fnxSession1")

    client_not_ok = _Client("nil")
    client_not_ok.execute_skill = lambda expr, **kwargs: SimpleNamespace(
        ok=False, errors=[], output="nil"
    )
    with pytest.raises(RuntimeError, match="maeCloseSession failed"):
        lifecycle.close_session(client_not_ok, "fnxSession1")


def test_open_gui_session_reuses_target_without_closing_earlier_windows(monkeypatch) -> None:
    client = _Client()
    closed: list[str] = []
    monkeypatch.setattr(lifecycle, "_close_background_sessions", lambda client: [])
    monkeypatch.setattr(
        lifecycle,
        "_get_session_windows",
        lambda client: [
            _window(session="fnxSession1", cell="OTHER", mode="editing", modified=False),
            _window(session="fnxSession2", cell="TARGET", mode="editing", modified=False),
        ],
    )
    monkeypatch.setattr(
        lifecycle, "close_gui_session", lambda client, s, **kwargs: closed.append(s)
    )

    assert lifecycle.open_gui_session(client, "LIB", "TARGET") == "fnxSession2"
    assert closed == []
    assert client.calls == []


def test_open_gui_session_checks_all_windows_before_closing_any(monkeypatch) -> None:
    client = _Client()
    closed: list[str] = []
    monkeypatch.setattr(lifecycle, "_close_background_sessions", lambda client: [])
    monkeypatch.setattr(
        lifecycle,
        "_get_session_windows",
        lambda client: [
            _window(session="fnxSession1", cell="OTHER", mode="editing", modified=False),
            _window(session="fnxSession2", cell="BROKEN", mode="unknown", modified=None),
        ],
    )
    monkeypatch.setattr(
        lifecycle, "close_gui_session", lambda client, s, **kwargs: closed.append(s)
    )

    with pytest.raises(RuntimeError, match="state is unknown"):
        lifecycle.open_gui_session(client, "LIB", "TARGET")
    assert closed == []
    assert client.calls == []

    monkeypatch.setattr(
        lifecycle,
        "_get_session_windows",
        lambda client: [
            _window(session="fnxSession1", cell="OTHER", mode="editing", modified=False),
            _window(session="fnxSession2", cell="DIRTY_RO", mode="reading", modified=True),
        ],
    )
    with pytest.raises(RuntimeError, match="read-only"):
        lifecycle.open_gui_session(client, "LIB", "TARGET")
    assert closed == []
    assert client.calls == []

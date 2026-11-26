"""Regression tests for GitHub issue #332 in ``virtuoso_bridge.cli``.

Covers:
1. ``_connected_virtuoso_pid`` / ``inspect-dialogs`` (without ``--pid``):
   never calls ``VirtuosoClient.from_env()``, never provisions daemon tokens,
   never executes SKILL, and always closes the created client/SSH tunnel.
2. ``cli_windows`` (``virtuoso-bridge windows``):
   coerces non-string/``nil`` ``w->davSession`` to ``""`` and guards
   ``maeGetSessions()`` with ``isCallable('maeGetSessions)`` so Schematic,
   Layout, CIW, and Maestro windows display ``Focused:`` and sessions cleanly.
3. Deterministic ``close()`` on normal return and exception paths across all
   CLI handlers that create ``VirtuosoClient`` or ``SSHRunner`` instances.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import virtuoso_bridge
from virtuoso_bridge import VirtuosoClient, cli, daemon_auth
from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.virtuoso import x11


# ---------------------------------------------------------------------------
# 1. _connected_virtuoso_pid / inspect-dialogs without --pid
# ---------------------------------------------------------------------------


def _signed_hello_exchange(token: str, pid: int, recorded: list[dict]):
    def exchange(self, payload, deadline):
        recorded.append(payload)
        assert payload["op"] == "hello"
        assert "skill" not in payload
        body = json.dumps({
            "proto": daemon_auth.PROTOCOL_VERSION,
            "auth": "on",
            "virtuoso_pid": pid,
        })
        mac = daemon_auth.response_mac(token, nonce=payload["nonce"], marker="\x02", body=body)
        return ("\x02" + mac + body).encode()

    return exchange


def test_connected_virtuoso_pid_uses_readonly_handshake_without_skill_or_provisioning(
    monkeypatch, capsys
) -> None:
    token = "a" * 64
    closed = []
    requests: list[dict] = []

    class _FakeSSH:
        gui_host = "eda-host"
        daemon_host = "eda-host"
        ssh_runner = object()
        _discover_instances = False

        def remote_cache_path(self):
            return None

        def read_daemon_token(self):
            return token

        def ensure_daemon_token(self):
            pytest.fail("inspect-dialogs must not provision daemon tokens")

        def close(self):
            closed.append("ssh")

    monkeypatch.setattr(cli, "load_vb_env", lambda **_kw: None)
    monkeypatch.setattr(cli, "_get_cli_profile", lambda: None)
    monkeypatch.setattr(SSHClient, "is_running", staticmethod(lambda profile=None: True))
    monkeypatch.setattr(SSHClient, "read_state", staticmethod(lambda profile=None: {"port": 65123}))
    monkeypatch.setattr(SSHClient, "from_env", classmethod(lambda cls, **kw: _FakeSSH()))
    monkeypatch.setattr(
        VirtuosoClient,
        "from_env",
        classmethod(lambda cls, **kw: pytest.fail("must not call VirtuosoClient.from_env()")),
    )
    monkeypatch.setattr(
        VirtuosoClient,
        "execute_skill",
        lambda self, *a, **kw: pytest.fail("inspect-dialogs must never send SKILL"),
    )
    monkeypatch.setattr(
        daemon_auth,
        "local_token_or_raise",
        lambda *a, **kw: pytest.fail("must not create local token"),
    )
    monkeypatch.setattr(
        daemon_auth,
        "update_remote_cache",
        lambda *a, **kw: pytest.fail("must not write remote cache"),
    )
    monkeypatch.setattr(
        VirtuosoClient,
        "_exchange_payload",
        _signed_hello_exchange(token, 4242, requests),
    )

    inspected: list[int] = []
    runner_closed: list[str] = []

    class _FakeRunner:
        def close(self):
            runner_closed.append("runner")

    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (_FakeRunner(), "designer"))
    monkeypatch.setattr(
        x11,
        "inspect_dialogs",
        lambda *a, pid, **kw: inspected.append(pid) or {
            "status": "blocked",
            "target": {"pid": pid, "display": ":7", "ciw_window": "0x10"},
            "dialogs": [{"window_id": "0x20", "title": "Modal Dialog"}],
            "diagnostics": [],
        },
    )

    rc = cli.cli_inspect_dialogs(json_output=True)

    assert rc == 2
    assert inspected == [4242]
    assert len(requests) == 1
    assert requests[0]["op"] == "hello"
    assert closed == ["ssh"]
    assert runner_closed == ["runner"]
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked"
    assert report["target"]["pid"] == 4242


def test_connected_virtuoso_pid_refreshes_stale_cached_token_readonly_and_closes(
    monkeypatch, tmp_path
) -> None:
    stale = "1" * 64
    fresh = "2" * 64
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"token": stale}), encoding="utf-8")
    if os.name == "posix":
        cache_file.chmod(0o600)
    closed = []
    reads = []

    class _FakeSSH:
        gui_host = "eda-host"
        daemon_host = "eda-host"
        ssh_runner = object()
        _discover_instances = False

        def remote_cache_path(self):
            return cache_file

        def read_daemon_token(self):
            reads.append("read")
            return fresh

        def ensure_daemon_token(self):
            pytest.fail("must not provision")

        def close(self):
            closed.append("ssh")

    def exchange(self, payload, deadline):
        expected_mac = daemon_auth.hello_mac(fresh, nonce=payload["nonce"])
        if payload.get("mac") != expected_mac:
            return b"\x15AuthError: invalid token"
        body = json.dumps({
            "proto": daemon_auth.PROTOCOL_VERSION,
            "auth": "on",
            "virtuoso_pid": 9001,
        })
        mac = daemon_auth.response_mac(fresh, nonce=payload["nonce"], marker="\x02", body=body)
        return ("\x02" + mac + body).encode()

    monkeypatch.setattr(cli, "_get_cli_profile", lambda: None)
    monkeypatch.setattr(SSHClient, "is_running", staticmethod(lambda profile=None: True))
    monkeypatch.setattr(SSHClient, "read_state", staticmethod(lambda profile=None: {"port": 65123}))
    monkeypatch.setattr(SSHClient, "from_env", classmethod(lambda cls, **kw: _FakeSSH()))
    monkeypatch.setattr(
        daemon_auth,
        "update_remote_cache",
        lambda *a, **kw: pytest.fail("must not mutate cache"),
    )
    monkeypatch.setattr(VirtuosoClient, "_exchange_payload", exchange)

    assert cli._connected_virtuoso_pid(timeout=5) == 9001
    assert reads == ["read"]
    assert closed == ["ssh"]


def test_connected_virtuoso_pid_closes_ssh_when_token_missing(monkeypatch) -> None:
    closed = []

    class _FakeSSH:
        gui_host = "eda-host"
        daemon_host = "eda-host"
        ssh_runner = None
        _discover_instances = False

        def read_daemon_token(self):
            return None

        def close(self):
            closed.append("ssh")

    monkeypatch.setattr(cli, "_get_cli_profile", lambda: None)
    monkeypatch.setattr(SSHClient, "is_running", staticmethod(lambda profile=None: True))
    monkeypatch.setattr(SSHClient, "read_state", staticmethod(lambda profile=None: {"port": 65123}))
    monkeypatch.setattr(SSHClient, "from_env", classmethod(lambda cls, **kw: _FakeSSH()))

    with pytest.raises(RuntimeError, match="no readable bridge daemon token"):
        cli._connected_virtuoso_pid(timeout=5)
    assert closed == ["ssh"]


def test_registered_entrypoint_inspect_dialogs_without_pid_does_not_provision(tmp_path) -> None:
    script = shutil.which("virtuoso-bridge", path=str(Path(sys.executable).parent))
    if script is None:
        pytest.skip("the virtuoso-bridge console script is not installed in this environment")
    env_file = tmp_path / "test.env"
    env_file.write_text("VB_REMOTE_HOST=127.0.0.1\n", encoding="utf-8")
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("VB_") and k not in {"HOME", "USERPROFILE", "VIRTUAL_ENV"}
    }
    env.update(
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        VB_LOG_DIR=str(tmp_path / "logs"),
        VB_STATE_DIR=str(tmp_path / "state"),
    )

    result = subprocess.run(
        [script, "inspect-dialogs", "--json", "--env", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "indeterminate"
    assert payload["target"] is None
    assert "pass --pid" in payload["diagnostics"][0]
    assert not (tmp_path / ".virtuoso-bridge" / "bridge_token").exists()


# ---------------------------------------------------------------------------
# 2. cli_windows: non-Maestro focused windows and guarded maeGetSessions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "focused_num, focused_name, skill_output, expected_focus, expected_sessions",
    [
        (
            "2",
            "Virtuoso® Schematic Editor L Editing: LIB cell schematic",
            '("2" "" nil)',
            "Focused: 2  Virtuoso® Schematic Editor L Editing: LIB cell schematic",
            None,
        ),
        (
            "2",
            "Virtuoso® Schematic Editor L Editing: LIB cell schematic",
            '("2" nil nil)',
            "Focused: 2  Virtuoso® Schematic Editor L Editing: LIB cell schematic",
            None,
        ),
        (
            "3",
            "Virtuoso® Layout Suite XL Editing: LIB cell layout",
            '("3" "" ("fnxSession0"))',
            "Focused: 3  Virtuoso® Layout Suite XL Editing: LIB cell layout",
            "Maestro sessions (1): fnxSession0",
        ),
        (
            "1",
            "Virtuoso® Studio IC23.1 - Log: /tmp/CDS.log",
            '("1" "" nil)',
            "Focused: 1  Virtuoso® Studio IC23.1 - Log: /tmp/CDS.log",
            None,
        ),
        (
            "4",
            "Virtuoso® ADE Assembler Editing: LIB tb maestro",
            '("4" "fnxSession0" ("fnxSession0" "fnxSession1"))',
            "Focused: 4  Virtuoso® ADE Assembler Editing: LIB tb maestro  [fnxSession0]",
            "Maestro sessions (2): fnxSession0, fnxSession1",
        ),
    ],
)
def test_cli_windows_displays_focused_window_and_guards_skill(
    monkeypatch,
    capsys,
    focused_num,
    focused_name,
    skill_output,
    expected_focus,
    expected_sessions,
) -> None:
    seen_skill: list[str] = []
    closed: list[bool] = []

    class _FakeWindowsClient:
        @classmethod
        def from_env(cls, *, profile=None):
            return cls()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append(True)

        def list_windows(self):
            return [
                {"num": "1", "name": "Virtuoso® Studio IC23.1 - Log: /tmp/CDS.log"},
                {"num": focused_num, "name": focused_name},
            ] if focused_num != "1" else [{"num": "1", "name": focused_name}]

        def execute_skill(self, expr: str, timeout=None):
            seen_skill.append(expr)
            return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=skill_output)

    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FakeWindowsClient)
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)

    rc = cli.cli_windows()

    out = capsys.readouterr().out
    assert rc == 0
    assert closed == [True]
    assert len(seen_skill) == 1
    assert 'if(w && stringp(w->davSession) w->davSession "")' in seen_skill[0]
    assert "if(isCallable('maeGetSessions) maeGetSessions() nil)" in seen_skill[0]
    assert expected_focus in out
    assert f"* {focused_num:>4}  {focused_name}" in out
    if expected_sessions is not None:
        assert expected_sessions in out
    else:
        assert "Maestro sessions" not in out


# ---------------------------------------------------------------------------
# 3. Deterministic close() across CLI handlers (normal and exception paths)
# ---------------------------------------------------------------------------


class _ClosableTracker:
    def __init__(self, **attrs) -> None:
        self.closed = 0
        for key, value in attrs.items():
            setattr(self, key, value)

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("raise_err", [False, True])
def test_cli_load_and_eval_always_close_client(tmp_path, monkeypatch, raise_err) -> None:
    il_file = tmp_path / "script.il"
    il_file.write_text("1+1\n", encoding="utf-8")

    def _run_or_raise(*_a, **_kw):
        if raise_err:
            raise RuntimeError("boom")
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output="2")

    load_client = _ClosableTracker(load_il=_run_or_raise)
    eval_client = _ClosableTracker(execute_skill=_run_or_raise)

    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)

    monkeypatch.setattr(
        virtuoso_bridge,
        "VirtuosoClient",
        SimpleNamespace(from_env=lambda **_kw: load_client),
    )
    if raise_err:
        with pytest.raises(RuntimeError, match="boom"):
            cli.cli_load(file=str(il_file), quiet=True)
    else:
        assert cli.cli_load(file=str(il_file), quiet=True) == 0
    assert load_client.closed == 1

    monkeypatch.setattr(
        virtuoso_bridge,
        "VirtuosoClient",
        SimpleNamespace(from_env=lambda **_kw: eval_client),
    )
    if raise_err:
        with pytest.raises(RuntimeError, match="boom"):
            cli.cli_eval(skill="1+1", stdin=False, quiet=True)
    else:
        assert cli.cli_eval(skill="1+1", stdin=False, quiet=True) == 0
    assert eval_client.closed == 1


@pytest.mark.parametrize("raise_err", [False, True])
def test_cli_docs_handlers_always_close_client(monkeypatch, capsys, raise_err) -> None:
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)

    def _make_client(method_name, value):
        def _call(*_a, **_kw):
            if raise_err:
                raise RuntimeError("docs boom")
            return value

        return _ClosableTracker(
            **{
                method_name: _call,
                "_skill_finder_cache_host": lambda: "eda-host",
            }
        )

    cases = [
        ("find_skill", [], lambda: cli.cli_find(query="dbOpen", mode="fuzzy", limit=5, include_desc=False, json_output=True)),
        ("get_skill_more_info", None, lambda: cli.cli_skill_info(func_name="dbOpen", json_output=True)),
        ("search_docs", {"doc_roots": ["/doc"], "results": []}, lambda: cli.cli_doc_search(query="net", doc_roots=[], limit=5, list_roots=False, json_output=True, rebuild_index=False)),
        ("doc_info", {"doc_roots": []}, lambda: cli.cli_doc_info(doc_roots=[], json_output=True)),
    ]
    for method_name, value, invoke in cases:
        client = _make_client(method_name, value)
        monkeypatch.setattr(cli, "_docs_client", lambda c=client: c)
        if raise_err:
            with pytest.raises(RuntimeError, match="docs boom"):
                invoke()
        else:
            invoke()
        assert client.closed == 1, f"{method_name} leaked client (raise_err={raise_err})"
    capsys.readouterr()


@pytest.mark.parametrize("raise_err", [False, True])
def test_cli_export_visio_and_sos_always_close_client(monkeypatch, capsys, raise_err) -> None:
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)

    visio_client = _ClosableTracker(get_current_design=lambda: (None, None, None))
    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", SimpleNamespace(from_env=lambda **_kw: visio_client))
    monkeypatch.setitem(cli._EXPORT_VISIO_OPTS, "lib", "LIB")
    monkeypatch.setitem(cli._EXPORT_VISIO_OPTS, "cell", "CELL")

    def _export(*_a, **_kw):
        if raise_err:
            raise ValueError("export boom")
        return SimpleNamespace(instances=(), nets={})

    monkeypatch.setattr("virtuoso_bridge.virtuoso.visio.export_schematic_to_visio", _export)
    if raise_err:
        with pytest.raises(ValueError, match="export boom"):
            cli.cli_export_visio()
    else:
        assert cli.cli_export_visio() == 0
    assert visio_client.closed == 1

    def _status(*_a, **_kw):
        if raise_err:
            raise RuntimeError("sos boom")
        return SimpleNamespace(to_dict=lambda: {"ok": True, "action": "status", "outcome": "success"})

    sos_client = _ClosableTracker(sos=SimpleNamespace(status_cellview=_status))
    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", SimpleNamespace(from_env=lambda **_kw: sos_client))
    if raise_err:
        with pytest.raises(RuntimeError, match="sos boom"):
            cli.cli_sos(action="status", lib="L", cell="C", view="schematic", json_output=True)
    else:
        assert cli.cli_sos(action="status", lib="L", cell="C", view="schematic", json_output=True) == 0
    assert sos_client.closed == 1
    capsys.readouterr()


@pytest.mark.parametrize("raise_err", [False, True])
def test_cli_x11_handlers_always_close_ssh_runner(monkeypatch, capsys, raise_err) -> None:
    monkeypatch.setattr(cli, "_load_cli_env", lambda: None)
    monkeypatch.setattr(cli, "load_vb_env", lambda **_kw: None)

    def _fail_or(value):
        def _fn(*_a, **_kw):
            if raise_err:
                raise RuntimeError("x11 boom")
            return value

        return _fn

    # dismiss-dialog (--legacy-bulk)
    runner = _ClosableTracker()
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (runner, "u"))
    monkeypatch.setattr(x11, "dismiss_dialogs", _fail_or([]))
    if raise_err:
        with pytest.raises(RuntimeError, match="x11 boom"):
            cli.cli_dismiss_dialog(legacy_bulk=True)
    else:
        assert cli.cli_dismiss_dialog(legacy_bulk=True) == 0
    assert runner.closed == 1

    # inspect-dialogs (note: catches Exception internally and returns indeterminate)
    runner = _ClosableTracker()
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (runner, "u"))
    monkeypatch.setattr(
        x11,
        "inspect_dialogs",
        _fail_or({
            "status": "clear",
            "target": {"pid": 42, "display": ":7", "ciw_window": "0x10"},
            "dialogs": [],
            "diagnostics": [],
        }),
    )
    assert cli.cli_inspect_dialogs(pid=42, json_output=True) == (1 if raise_err else 0)
    assert runner.closed == 1

    # list-windows
    runner = _ClosableTracker()
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (runner, "u"))
    monkeypatch.setattr(x11, "list_windows", _fail_or([]))
    if raise_err:
        with pytest.raises(RuntimeError, match="x11 boom"):
            cli.cli_list_windows(json_output=True)
    else:
        assert cli.cli_list_windows(json_output=True) == 0
    assert runner.closed == 1

    # dismiss-window
    runner = _ClosableTracker()
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (runner, "u"))
    monkeypatch.setattr(
        x11,
        "dismiss_window",
        _fail_or([{"dismissed": "0x10", "action": "enter", "status": "closed", "closure_confirmed": True, "still_mapped": False}]),
    )
    if raise_err:
        with pytest.raises(RuntimeError, match="x11 boom"):
            cli.cli_dismiss_window(window_id="0x10")
    else:
        assert cli.cli_dismiss_window(window_id="0x10") == 0
    assert runner.closed == 1

    # gui
    runner = _ClosableTracker()
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (runner, "u"))

    class _FakeGui:
        def __init__(self, *_a, **_kw):
            pass

        def missing_tools(self):
            return []

        def windows(self, _pat):
            if raise_err:
                raise RuntimeError("gui boom")
            return []

    monkeypatch.setattr("virtuoso_bridge.virtuoso.x11_gui.X11Gui", _FakeGui)
    if raise_err:
        with pytest.raises(RuntimeError, match="gui boom"):
            cli.cli_gui(action="windows")
    else:
        assert cli.cli_gui(action="windows") == 0
    assert runner.closed == 1

    # bootstrap
    runner = _ClosableTracker()
    monkeypatch.setattr(cli, "_make_ssh_runner", lambda: (runner, "u"))
    monkeypatch.setattr(SSHClient, "read_state", staticmethod(lambda profile=None: {"setup_path": "/s/setup.il"}))
    monkeypatch.setattr(x11, "bootstrap_ciw", _fail_or([{"error": "refused"}]))
    if raise_err:
        with pytest.raises(RuntimeError, match="x11 boom"):
            cli.cli_bootstrap(window_id="0x10")
    else:
        assert cli.cli_bootstrap(window_id="0x10") == 1
    assert runner.closed == 1
    capsys.readouterr()

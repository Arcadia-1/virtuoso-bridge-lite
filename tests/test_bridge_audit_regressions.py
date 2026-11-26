"""Regression tests for #303/#304 fixes in bridge.py and daemon_27 error handling."""

from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient


_RESOURCES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "virtuoso_bridge"
    / "virtuoso"
    / "basic"
    / "resources"
)


def test_ciw_print_uses_format_string_for_percent_messages(monkeypatch) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=9, timeout=5)
    sent: list[str] = []
    monkeypatch.setattr(
        client,
        "execute_skill",
        lambda skill, timeout=None, **_kw: sent.append(skill)
        or VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t"),
    )

    result = client.ciw_print('gain 100% %s "ok"')

    assert result.ok
    assert sent == ['printf("%s\\n" "gain 100% %s \\"ok\\"")']


def test_load_il_ciw_log_uses_format_string_for_percent_paths(monkeypatch) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=9, timeout=5, log_to_ciw=True)
    logged: list[str] = []
    monkeypatch.setattr(
        client,
        "execute_skill",
        lambda skill, timeout=None, **_kw: VirtuosoResult(
            status=ExecutionStatus.SUCCESS, output="t"
        ),
    )
    monkeypatch.setattr(
        client,
        "ciw_log",
        lambda skill, timeout=None: logged.append(skill)
        or VirtuosoResult(status=ExecutionStatus.SUCCESS, output="t"),
    )

    result = client.load_il("/remote/100%gain_%s.il")

    assert result.ok
    assert logged == ['printf("%s\\n" "[RAMIC] loaded /remote/100%gain_%s.il")']


def test_warm_remote_session_copies_tunnel_daemon_token() -> None:
    token = "ab" * 32

    class _FakeTunnel:
        port = 65123
        is_tunnel_alive = True

        def __init__(self) -> None:
            self.daemon_token: str | None = None

        def warm(self, timeout: int = 10) -> None:
            self.daemon_token = token

    tunnel = _FakeTunnel()
    client = VirtuosoClient(host="127.0.0.1", port=9, tunnel=tunnel)
    assert client.daemon_token is None

    result = client.warm_remote_session(timeout=5)

    assert result.ok
    assert client.port == 65123
    assert client.daemon_token == token

    # An already-set client token is preserved.
    existing = "cd" * 32
    client2 = VirtuosoClient(host="127.0.0.1", port=9, tunnel=tunnel, daemon_token=existing)
    assert client2.warm_remote_session(timeout=5).ok
    assert client2.daemon_token == existing


def test_prepare_il_path_keys_same_basename_files_uniquely(tmp_path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    file_a = dir_a / "init.il"
    file_b = dir_b / "init.il"
    file_a.write_text('printf("A\\n")\n', encoding="utf-8")
    file_b.write_text('printf("B\\n")\n', encoding="utf-8")

    remote_files: dict[str, str] = {}
    uploads: list[tuple[str, str]] = []

    class _FakeTunnel:
        ssh_runner = object()
        remote_work_dir = "/tmp/virtuoso_bridge_user/work"
        port = 65432

        def upload_text(self, text: str, remote_path: str) -> CommandResult:
            uploads.append((remote_path, text))
            remote_files[remote_path] = text
            return CommandResult(returncode=0, stdout="", stderr="")

    client = VirtuosoClient(host="127.0.0.1", port=65432, tunnel=_FakeTunnel())

    remote_a1, uploaded_a1 = client._prepare_il_path(file_a)
    remote_b1, uploaded_b1 = client._prepare_il_path(file_b)
    remote_a2, uploaded_a2 = client._prepare_il_path(file_a)

    assert uploaded_a1 is True
    assert uploaded_b1 is True
    assert uploaded_a2 is False
    assert remote_a1 != remote_b1
    assert remote_a2 == remote_a1
    assert remote_a1.endswith("_init.il")
    assert remote_b1.endswith("_init.il")
    assert remote_files[remote_a1] == 'printf("A\\n")\n'
    assert remote_files[remote_b1] == 'printf("B\\n")\n'
    assert len(uploads) == 2

    # Modifying file_a re-uploads to file_a's path without overwriting file_b.
    file_a.write_text('printf("A2\\n")\n', encoding="utf-8")
    remote_a3, uploaded_a3 = client._prepare_il_path(file_a)
    assert uploaded_a3 is True
    assert remote_a3 == remote_a1
    assert remote_files[remote_a1] == 'printf("A2\\n")\n'
    assert remote_files[remote_b1] == 'printf("B\\n")\n'


def test_py27_resource_files_are_pure_ascii() -> None:
    for name in (
        "ramic_registry.py",
        "ramic_bridge_daemon_27.py",
        "ramic_deploy.py",
        "ramic_paths.py",
        "ramic_request_recovery.py",
    ):
        (_RESOURCES / name).read_bytes().decode("ascii")


def test_py27_daemon_except_handlers_format_non_ascii_without_dropping_socket(
    monkeypatch, tmp_path
) -> None:
    fcntl_stub = SimpleNamespace(fcntl=lambda *args, **kwargs: 0, F_GETFL=3, F_SETFL=4)
    monkeypatch.setitem(sys.modules, "fcntl", fcntl_stub)
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(fileno=lambda: 0, buffer=SimpleNamespace(read=lambda n=1: b"")),
    )
    token_path = tmp_path / "bridge_token"
    token_path.write_text("ab" * 32 + "\n", encoding="ascii")
    monkeypatch.setenv("RB_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(sys, "argv", ["daemon", "127.0.0.1", "1"])

    spec = importlib.util.spec_from_file_location(
        "vb_daemon27_nonascii_" + uuid.uuid4().hex,
        str(_RESOURCES / "ramic_bridge_daemon_27.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Simulate Python 2.7 unicode vs str behavior:
    # 1) Non-ASCII Python 2.7 str has .encode() that raises UnicodeDecodeError;
    #    _to_utf8_bytes must NOT call .encode("utf-8") on str.
    # 2) Non-ASCII Python 2.7 unicode raises UnicodeEncodeError on str(exc);
    #    _format_exception must fall back to unicode(exc).encode("utf-8").
    class _Py2Unicode(str):
        def __new__(cls, value=""):
            if isinstance(value, BaseException):
                value = value.args[0] if value.args else ""
            return super().__new__(cls, value)

        def encode(self, encoding="utf-8", errors="strict"):
            return super().encode(encoding, errors)

    class _Py2Str(str):
        def encode(self, encoding="utf-8", errors="strict"):
            raise UnicodeDecodeError("ascii", b"\xc3\xa9", 0, 1, "ordinal not in range(128)")

    class _Py2UnicodeValueError(ValueError):
        def __str__(self):
            raise UnicodeEncodeError("ascii", "bad \u00e9", 4, 5, "ordinal not in range(128)")

    class _Py2UnicodeRuntimeError(RuntimeError):
        def __str__(self):
            raise UnicodeEncodeError("ascii", "boom \u00e9", 5, 6, "ordinal not in range(128)")

    module.unicode = _Py2Unicode
    module.basestring = (str, bytes)

    assert module._to_utf8_bytes(_Py2Str("bad \u00e9")) == "bad \u00e9"
    assert module._format_exception(_Py2UnicodeValueError(_Py2Unicode("bad \u00e9"))) == b"bad \xc3\xa9"

    class _FakeConn:
        def __init__(self) -> None:
            self.sent: list[object] = []
            self.closed = False

        def settimeout(self, _value) -> None:
            pass

        def sendall(self, data) -> None:
            self.sent.append(data)

        def shutdown(self, _how) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    # Non-ASCII str in ValueError and RuntimeError (must not invoke .encode("utf-8") on str).
    for exc, prefix in (
        (ValueError(_Py2Str("bad \u00e9")), "\x15JSONDecodeError: "),
        (RuntimeError(_Py2Str("boom \u00e9")), "\x15"),
    ):
        monkeypatch.setattr(module, "_recv_request", lambda _c, _e=exc: (_ for _ in ()).throw(_e))
        conn = _FakeConn()
        module.handle_external_connection(conn, ("127.0.0.1", 0))
        assert len(conn.sent) == 1
        assert conn.sent[0].startswith(prefix)
        assert "\u00e9" in conn.sent[0]
        assert conn.closed

    # Non-ASCII unicode exception where str(exc) raises UnicodeEncodeError.
    class _Py2UnicodeWire(str):
        def __new__(cls, value=""):
            if isinstance(value, BaseException):
                value = value.args[0] if value.args else ""
            return super().__new__(cls, value)

        def encode(self, encoding="utf-8", errors="strict"):
            # In Python 2.7 .encode("utf-8") returns str (bytes), concatenable with "\x15".
            return str(self)

    module.unicode = _Py2UnicodeWire
    for exc, prefix in (
        (_Py2UnicodeValueError(_Py2UnicodeWire("bad \u00e9")), "\x15JSONDecodeError: "),
        (_Py2UnicodeRuntimeError(_Py2UnicodeWire("boom \u00e9")), "\x15"),
    ):
        monkeypatch.setattr(module, "_recv_request", lambda _c, _e=exc: (_ for _ in ()).throw(_e))
        conn = _FakeConn()
        module.handle_external_connection(conn, ("127.0.0.1", 0))
        assert conn.sent == [prefix + exc.args[0]]
        assert conn.closed


def test_py27_daemon_accept_and_signed_busy_rejection(monkeypatch, tmp_path) -> None:
    import errno
    import json
    import threading
    from virtuoso_bridge import daemon_auth

    fcntl_stub = SimpleNamespace(fcntl=lambda *args, **kwargs: 0, F_GETFL=3, F_SETFL=4)
    monkeypatch.setitem(sys.modules, "fcntl", fcntl_stub)
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(fileno=lambda: 0, buffer=SimpleNamespace(read=lambda n=1: b"")),
    )
    token = "ab" * 32
    token_path = tmp_path / "bridge_token"
    token_path.write_text(token + "\n", encoding="ascii")
    monkeypatch.setenv("RB_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(sys, "argv", ["daemon", "127.0.0.1", "1"])

    spec = importlib.util.spec_from_file_location(
        "vb_daemon27_accept_busy_" + uuid.uuid4().hex,
        str(_RESOURCES / "ramic_bridge_daemon_27.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.unicode = bytes  # no str instance is bytes; keeps _to_utf8_bytes working in Py3
    module.basestring = (str, bytes)
    module.BRIDGE_TOKEN = token.encode("ascii")
    # Mirror Python 2.7 _frame where str is bytes (encode text to bytes for hmac in Py3).
    monkeypatch.setattr(
        module,
        "_mac_hex",
        lambda *parts: daemon_auth._mac_hex(
            token, *[p.decode("latin1") if isinstance(p, bytes) else p for p in parts]
        ),
    )

    sleeps: list[float] = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    class _FakeServer:
        def __init__(self, exc):
            self._exc = exc

        def accept(self):
            raise self._exc

    for transient in (errno.EINTR, errno.ECONNABORTED, errno.ECONNRESET, errno.EAGAIN):
        assert module._accept_next(_FakeServer(OSError(transient, "transient"))) is None
    assert sleeps == []
    assert module._accept_next(_FakeServer(OSError(errno.EMFILE, "too many files"))) is None
    assert module._accept_next(_FakeServer(OSError(errno.ENFILE, "file table"))) is None
    assert sleeps == [0.05, 0.05]
    assert module._accept_next(_FakeServer(OSError(errno.EBADF, "closed"))) is module._ACCEPT_STOP

    class _FakeConn:
        def __init__(self, payload: str) -> None:
            self._chunks = [payload, ""]
            self.sent: list[str] = []
            self.closed = False

        def settimeout(self, _value) -> None:
            pass

        def recv(self, _n):
            return self._chunks.pop(0) if self._chunks else ""

        def sendall(self, data) -> None:
            self.sent.append(data)

        def shutdown(self, _how) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    # Validly signed hello -> signed BusyError, slot released, nonce NOT consumed.
    nonce = "12" * 16
    hello_payload = json.dumps({
        "proto": 1,
        "op": "hello",
        "nonce": nonce,
        "mac": daemon_auth.hello_mac(token, nonce=nonce),
    })
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(False)
    conn = _FakeConn(hello_payload)
    module._reject_connection_limit(conn, sem)
    assert conn.closed
    assert sem.acquire(False)  # released
    assert len(conn.sent) == 1
    verified = daemon_auth.verify_response(conn.sent[0], token, nonce)
    assert verified == "\x15BusyError: daemon connection limit reached"
    assert nonce not in module._NONCE_MARK

    # Wrong token -> unsigned AuthError.
    bad_payload = json.dumps({
        "proto": 1,
        "op": "hello",
        "nonce": nonce,
        "mac": daemon_auth.hello_mac("cd" * 32, nonce=nonce),
    })
    conn_bad = _FakeConn(bad_payload)
    module._reject_connection_limit(conn_bad, sem)
    assert conn_bad.closed
    assert conn_bad.sent and conn_bad.sent[0].startswith("\x15AuthError:")

    # Backlog and rejector tuning + no unsigned BusyError when reject_slots is exhausted under auth.
    assert module._LISTEN_BACKLOG == 64
    assert module._MAX_BUSY_REJECTORS == 32
    assert module._BUSY_REJECT_TIMEOUT == 0.5
    exhausted = threading.BoundedSemaphore(1)
    assert exhausted.acquire(False)
    conn_overflow = _FakeConn("")
    module._dispatch_overflow_connection(conn_overflow, exhausted)
    assert conn_overflow.sent == []
    assert conn_overflow.closed is True

    monkeypatch.setattr(module, "BRIDGE_TOKEN", None)
    conn_unauth = _FakeConn("")
    module._dispatch_overflow_connection(conn_unauth, exhausted)
    assert conn_unauth.sent == [module._BUSY_LIMIT_REPLY]
    assert conn_unauth.closed is True


def test_ramic_deploy_redacts_anchor_in_both_str_and_py2_unicode(monkeypatch) -> None:
    from virtuoso_bridge.virtuoso.basic.resources import ramic_deploy

    class _FakePy2Unicode:
        """Non-str text object modeling Python 2 unicode (isinstance(..., str) is False)."""

        def __init__(self, text: str) -> None:
            self.text = text

        def replace(self, old: str, new: str) -> "_FakePy2Unicode":
            return _FakePy2Unicode(self.text.replace(old, new))

        def __str__(self) -> str:
            return self.text

    assert not isinstance(_FakePy2Unicode("x"), str)
    monkeypatch.setattr(ramic_deploy, "_TEXT_TYPES", (str, _FakePy2Unicode))

    closed_fds: list[int] = []
    monkeypatch.setattr(ramic_deploy, "private_directory", lambda _root, create=True: 42)
    monkeypatch.setattr(ramic_deploy.os, "close", closed_fds.append)

    # 1) Non-str unicode-like exception argument (Python 2 unicode).
    def fail_runtime(dest, files, replace, timeout, root_fd):
        raise RuntimeError(_FakePy2Unicode(f"Differs at {dest}"))

    monkeypatch.setattr(ramic_deploy, "_publish_at", fail_runtime)
    with pytest.raises(RuntimeError) as caught_rt:
        ramic_deploy.publish("/home/alice/private/daemon", {"daemon.py": "pass"})
    assert "/proc/self/fd/" not in str(caught_rt.value)
    assert str(caught_rt.value) == "Differs at /home/alice/private/daemon"

    # 2) OSError strerror/filename/filename2 redaction.
    def fail_oserror(dest, files, replace, timeout, root_fd):
        raise OSError(
            13,
            f"Permission denied: {dest}/daemon.py",
            f"{dest}/daemon.py",
            None,
            f"{dest}/backup",
        )

    monkeypatch.setattr(ramic_deploy, "_publish_at", fail_oserror)
    with pytest.raises(OSError) as caught:
        ramic_deploy.publish("/home/alice/private/daemon", {"daemon.py": "pass"})

    exc = caught.value
    assert closed_fds == [42, 42]
    assert "/proc/self/fd/" not in str(exc)
    assert exc.strerror == "Permission denied: /home/alice/private/daemon/daemon.py"
    assert exc.filename == "/home/alice/private/daemon/daemon.py"
    assert exc.filename2 == "/home/alice/private/daemon/backup"


def test_get_current_design_uses_cellview_slots_not_dd_path(monkeypatch) -> None:
    client = VirtuosoClient(host="127.0.0.1", port=9, timeout=5)
    sent: list[str] = []

    outputs = iter([
        VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output='("logical_alias_lib" "innocent_sch" "schematic")',
        ),
        VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output='("lib with space" "cell\\"1" "layout")',
        ),
        VirtuosoResult(
            status=ExecutionStatus.SUCCESS,
            output='("unsaved_lib" "unsaved_cell" nil)',
        ),
        VirtuosoResult(status=ExecutionStatus.SUCCESS, output="nil"),
        VirtuosoResult(status=ExecutionStatus.ERROR, errors=["boom"]),
    ])

    def fake_exec(skill, timeout=None, **_kw):
        sent.append(skill)
        return next(outputs)

    monkeypatch.setattr(client, "execute_skill", fake_exec)

    assert client.get_current_design() == ("logical_alias_lib", "innocent_sch", "schematic")
    assert "geGetEditCellView()" in sent[0]
    assert "cv~>libName" in sent[0] and "cv~>cellName" in sent[0] and "cv~>viewName" in sent[0]
    assert "ddGetObjReadPath" not in sent[0]

    assert client.get_current_design() == ("lib with space", 'cell"1', "layout")
    assert client.get_current_design() == ("unsaved_lib", "unsaved_cell", None)
    assert client.get_current_design() == (None, None, None)
    assert client.get_current_design() == (None, None, None)


def test_screenshot_rejects_bool_target() -> None:
    client = VirtuosoClient(host="127.0.0.1", port=9, timeout=5)
    for bad in (True, False):
        with pytest.raises(ValueError, match="bool"):
            client.screenshot(target=bad)


def test_screenshot_int_target_checks_ciw_handles_nil_save_uses_gui_runner_and_sanitizes(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/vb_scratch")
    monkeypatch.setenv("VB_CLIENT_ID", "laptop")

    class _DaemonRunner:
        def run_command(self, cmd, **kw):
            pytest.fail(f"daemon runner must not be used for screenshot: {cmd}")

        def download(self, *a, **kw):
            pytest.fail("daemon runner must not download screenshots")

    class _GuiRunner:
        user = "alice"

        def __init__(self) -> None:
            self.commands: list[str] = []
            self.downloads: list[tuple[str, Path]] = []

        def run_command(self, cmd: str, **kw) -> CommandResult:
            self.commands.append(cmd)
            if cmd == 'getent passwd "$(id -u)" | cut -d: -f6':
                return CommandResult(0, "/home/alice\n", "")
            return CommandResult(0, "", "")

        def download(self, remote_path: str, local_path: Path, **kw) -> CommandResult:
            self.downloads.append((remote_path, Path(local_path)))
            Path(local_path).write_bytes(b"png-bytes")
            return CommandResult(0, "", "")

    gui_runner = _GuiRunner()
    tunnel = SimpleNamespace(
        _ssh_runner=_DaemonRunner(),
        ssh_runner=_DaemonRunner(),
        gui_runner=gui_runner,
        _remote_user="alice",
        _profile=None,
    )
    client = VirtuosoClient(host="127.0.0.1", port=9, tunnel=tunnel)
    sent_skills: list[str] = []

    # 1) target=1 checks hiGetCIWindow() first, expands ~/vb_scratch, uses gui_runner.
    def ok_exec(skill, timeout=None, **_kw):
        sent_skills.append(skill)
        return VirtuosoResult(status=ExecutionStatus.SUCCESS, output='"ok"')

    monkeypatch.setattr(client, "execute_skill", ok_exec)
    out1 = tmp_path / "ciw.png"
    res1 = client.screenshot(output=out1, target=1)
    assert res1.ok
    assert out1.read_bytes() == b"png-bytes"
    assert "ciw=hiGetCIWindow()" in sent_skills[0]
    assert "ciw~>windowNum==1" in sent_skills[0]
    assert 'if(hiWindowSaveImage(' in sent_skills[0]
    assert "/home/alice/vb_scratch/virtuoso_bridge_alice/laptop/screenshots/1_" in sent_skills[0]
    assert "~/" not in sent_skills[0]
    assert any(
        cmd.startswith("mkdir -p /home/alice/vb_scratch/virtuoso_bridge_alice/laptop/screenshots")
        for cmd in gui_runner.commands
    )
    assert len(gui_runner.downloads) == 1

    # 2) hiWindowSaveImage returning nil -> clean ExecutionStatus.ERROR, no download attempted.
    gui_runner.downloads.clear()
    monkeypatch.setattr(
        client,
        "execute_skill",
        lambda skill, timeout=None, **_kw: VirtuosoResult(
            status=ExecutionStatus.SUCCESS, output='"error: image save failed"'
        ),
    )
    res2 = client.screenshot(output=tmp_path / "fail.png", target=1)
    assert res2.status == ExecutionStatus.ERROR
    assert "hiWindowSaveImage failed" in res2.errors[0]
    assert gui_runner.downloads == []

    # 3) Path-traversal target string is sanitized before forming remote_path.
    import re as _re

    sent_skills.clear()
    monkeypatch.setattr(client, "execute_skill", ok_exec)
    res3 = client.screenshot(output=tmp_path, target="../../etc/passwd")
    assert res3.ok
    saved_path = _re.search(r'\?path "([^"]+)"', sent_skills[0]).group(1)
    assert ".." not in saved_path
    assert saved_path.startswith("/home/alice/vb_scratch/virtuoso_bridge_alice/laptop/screenshots/etc_passwd_")
    remote_dl, local_dl = gui_runner.downloads[0]
    assert remote_dl == saved_path
    assert local_dl.parent == tmp_path
    assert local_dl.name.startswith("etc_passwd_")


def test_prepare_il_path_expands_tilde_scratch_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/vb_scratch")
    monkeypatch.setenv("VB_CLIENT_ID", "laptop")

    il_file = tmp_path / "script.il"
    il_file.write_text("1+1\n", encoding="utf-8")

    uploads: list[tuple[str, str]] = []

    class _Runner:
        def run_command(self, cmd: str, **kw) -> CommandResult:
            if cmd == 'getent passwd "$(id -u)" | cut -d: -f6':
                return CommandResult(0, "/home/alice\n", "")
            return CommandResult(0, "", "")

    class _Tunnel:
        ssh_runner = _Runner()
        remote_work_dir = None
        _remote_user = "alice"
        _profile = None
        port = 65432

        def upload_text(self, text: str, remote_path: str) -> CommandResult:
            uploads.append((remote_path, text))
            return CommandResult(0, "", "")

    client = VirtuosoClient(host="127.0.0.1", port=65432, tunnel=_Tunnel())
    remote_path, uploaded = client._prepare_il_path(il_file)
    assert uploaded is True
    assert remote_path.startswith("/home/alice/vb_scratch/virtuoso_bridge_alice/laptop/")
    assert "~/" not in remote_path
    assert uploads[0][0] == remote_path

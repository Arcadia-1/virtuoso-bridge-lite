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

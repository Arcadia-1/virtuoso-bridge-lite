"""How the daemon wraps SKILL requests before sending them to the CIW.

Multi-line SKILL requests must surface the real SKILL error.

The daemon loads multi-line code from a temp file. An error inside a loaded
file reaches the caller only as load's "error while loading file ... at line
N", so the file runs the code under errset and the command re-raises the
captured error after load() returns.
"""

from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path

import pytest

from tests.test_recoverable_daemon import _import_daemon, _resources
from virtuoso_bridge.virtuoso.basic.composition import compose_skill_script
from virtuoso_bridge.virtuoso.basic.resources.ramic_request_recovery import (
    REPLY_TAG_PREFIX,
    RequestNotStarted,
)

DAEMON_CONSTANTS = (
    "_INLINE_SKILL_MAX_BYTES",
    "_MULTILINE_PREFIX",
    "_MULTILINE_SUFFIX",
    "_MULTILINE_RESULT",
)


def _constants(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in DAEMON_CONSTANTS:
                values[name] = ast.literal_eval(node.value)
    return values


def _balanced(text: str) -> bool:
    depth, in_string, escaped, comment = 0, False, False, False
    for char in text:
        if comment:
            if char == "\n":
                comment = False
            continue
        if in_string:
            escaped = char == "\\" and not escaped
            if char == '"' and not escaped:
                in_string = False
            continue
        if char == ";":
            comment = True
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


class _Py2Unicode:
    """Minimal Python-2 unicode stand-in whose UTF-8 bytes are a str."""

    def __init__(self, text: str) -> None:
        self._text = text

    def encode(self, encoding: str) -> str:
        # In Python 2, unicode.encode("utf-8") returns a byte-string (str)
        # whose len() is the UTF-8 byte count.  Preserve that byte count in
        # Python 3 via latin-1 1:1 byte-to-char mapping so _prepare_skill's
        # "\n"/"\r"/";"/len() checks and file writes mirror Python 2.7.
        return self._text.encode(encoding).decode("latin-1")


class _Py2OsShim:
    """Adapt os.fdopen(fd, 'wb') so Py2.7 _prepare_skill runs under Py3."""

    def fdopen(self, fd: int, mode: str = "r"):
        if mode == "wb":
            return os.fdopen(fd, "w", encoding="latin-1", newline="")
        return os.fdopen(fd, mode)

    def unlink(self, path: str) -> None:
        os.unlink(path)


def _load_py27_prepare_skill():
    path = _resources() / "ramic_bridge_daemon_27.py"
    source = path.read_bytes().decode("ascii")
    tree = ast.parse(source)
    constants = _constants(path)
    func = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_prepare_skill"
    )
    namespace = {
        "os": _Py2OsShim(),
        "tempfile": tempfile,
        "unicode": _Py2Unicode,
        "REPLY_TAG_PREFIX": REPLY_TAG_PREFIX,
        "RequestNotStarted": RequestNotStarted,
        **constants,
    }
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_prepare_skill"], constants


def _prepare(monkeypatch, tmp_path, version: str, code, tag: str | None = None):
    if version == "py3":
        daemon = _import_daemon(monkeypatch, tmp_path)
        send, path = daemon._prepare_skill(code, tag=tag)
        return send.decode("utf-8"), path, {
            name: getattr(daemon, name) for name in DAEMON_CONSTANTS
        }
    prepare_fn, constants = _load_py27_prepare_skill()
    send, path = prepare_fn(code, tag=tag)
    return send, path, constants


def test_multiline_request_runs_code_under_errset_and_reraises_after_load(monkeypatch, tmp_path):
    daemon = _import_daemon(monkeypatch, tmp_path)
    code = 'let((x)\n  x = car(1) ; comment ) "quoted )"\n)'

    send, path = daemon._prepare_skill(code)
    try:
        written = Path(path).read_text(encoding="utf-8")
    finally:
        os.unlink(path)
    command = send.decode("utf-8")

    assert written == daemon._MULTILINE_PREFIX + code + daemon._MULTILINE_SUFFIX
    assert "errset(progn(\n" in written and "\n) t)" in written
    assert "_vb_eval_err = unless(_vb_eval_ok errset.errset)" in written
    # State is reset before load(), so a file that fails to parse can never
    # re-raise the previous request's error; the re-raise happens after load.
    reset = command.index("_vb_eval_ok = _vb_eval_err = nil")
    load = command.index(f'load("{path}")')
    reraise = command.index('error("%s"')
    assert reset < load < reraise
    assert "else car(_vb_eval_ok)" in command
    assert command.endswith("\n") and command.count("\n") == 1
    assert _balanced(command.strip())
    assert _balanced(daemon._MULTILINE_PREFIX + daemon._MULTILINE_SUFFIX)


def test_single_line_requests_do_not_use_the_temp_file(monkeypatch, tmp_path):
    daemon = _import_daemon(monkeypatch, tmp_path)

    send, path = daemon._prepare_skill("1+2")

    assert path is None
    assert "load(" not in send.decode("utf-8")


def test_single_line_request_may_hold_several_forms(monkeypatch, tmp_path):
    daemon = _import_daemon(monkeypatch, tmp_path)

    send, _path = daemon._prepare_skill("x = 1 x + 41")

    assert send == b"let(((__vb_r progn(x = 1 x + 41))) hiFlush() __vb_r)\n"
    py27 = (_resources() / "ramic_bridge_daemon_27.py").read_text(encoding="utf-8")
    assert "'let(((__vb_r progn(' + skill_code + '))) hiFlush() __vb_r)\\n'" in py27


def test_python27_daemon_uses_identical_multiline_wrapping():
    py3 = _constants(_resources() / "ramic_bridge_daemon_3.py")
    py27 = _constants(_resources() / "ramic_bridge_daemon_27.py")

    assert set(py3) == set(DAEMON_CONSTANTS)
    assert py3["_INLINE_SKILL_MAX_BYTES"] == 2048
    assert py27 == py3


@pytest.mark.parametrize("version", ["py3", "py27"])
@pytest.mark.parametrize(
    "code",
    [
        "x = 1 ; init",
        'printf("a;b")',
        "x = 1\ry = 2",
        "x = 1\r",
        '"' + ("a" * 2047) + '"',  # 2049 bytes (> 2048)
    ],
)
def test_single_line_comments_cr_and_long_payloads_spill_to_temp_file(
    monkeypatch, tmp_path, version, code
):
    command, path, constants = _prepare(monkeypatch, tmp_path, version, code)
    assert path is not None
    try:
        written = Path(path).read_bytes().decode("latin-1" if version == "py27" else "utf-8")
    finally:
        os.unlink(path)

    escaped_path = path.replace("\\", "/")
    assert written == constants["_MULTILINE_PREFIX"] + code + constants["_MULTILINE_SUFFIX"]
    assert f'load("{escaped_path}")' in command
    assert "\r" not in command
    assert command.endswith("\n") and command.count("\n") == 1
    assert _balanced(command.strip())
    # Closing paren of errset(progn(\n...\n) t) is never commented out by ';'.
    assert _balanced(written)


@pytest.mark.parametrize("version", ["py3", "py27"])
def test_multibyte_utf8_over_2048_bytes_spills_even_under_2048_chars(
    monkeypatch, tmp_path, version
):
    text = '"' + ("\u00e9" * 1025) + '"'  # 1027 chars, 2052 UTF-8 bytes
    code = text if version == "py3" else _Py2Unicode(text)

    command, path, _constants_map = _prepare(monkeypatch, tmp_path, version, code)
    assert path is not None
    try:
        raw = Path(path).read_bytes()
    finally:
        os.unlink(path)

    escaped_path = path.replace("\\", "/")
    assert text.encode("utf-8") in raw
    assert f'load("{escaped_path}")' in command
    assert command.endswith("\n") and command.count("\n") == 1


@pytest.mark.parametrize("version", ["py3", "py27"])
def test_exact_2048_byte_plain_single_line_stays_inline(monkeypatch, tmp_path, version):
    code = '"' + ("a" * 2046) + '"'  # exactly 2048 bytes, no \n, \r, or ;
    assert len(code) == 2048

    command, path, _constants_map = _prepare(monkeypatch, tmp_path, version, code)

    assert path is None
    assert "load(" not in command
    assert command == f"let(((__vb_r progn({code}))) hiFlush() __vb_r)\n"


@pytest.mark.parametrize("version", ["py3", "py27"])
def test_tagged_spilled_single_line_preserves_reply_tag_and_single_newline(
    monkeypatch, tmp_path, version
):
    tag = "0123456789abcdef"
    command, path, _constants_map = _prepare(
        monkeypatch, tmp_path, version, "x = 1 ; init", tag=tag
    )
    assert path is not None
    os.unlink(path)

    assert command.startswith(f"{REPLY_TAG_PREFIX}{tag} _vb_eval_ok = _vb_eval_err = nil load(")
    assert command.endswith("\n") and command.count("\n") == 1


def test_compose_skill_script_isolates_trailing_skill_comments_from_closing_paren():
    multi = compose_skill_script(["x = 1", "y = 2 ; comment"])
    assert multi == "progn(\nx = 1\ny = 2 ; comment\n)"
    assert _balanced(multi)

    single = compose_skill_script(["x = 1 ; trailing comment"])
    assert single == "progn(\nx = 1 ; trailing comment\n)"
    assert _balanced(single)

    assert compose_skill_script(["x = 1", "y = 2"], wrap_in_progn=False) == "x = 1\ny = 2"
    assert compose_skill_script(["progn(x = 1)"]) == "progn(x = 1)"
    with pytest.raises(ValueError, match="At least one SKILL command is required"):
        compose_skill_script(["   ", ""])


def test_ramic_bridge_il_buffers_lines_and_resets_across_sessions():
    il = (_resources() / "ramic_bridge.il").read_text(encoding="utf-8")

    # Reload-safe buffer globals initialized at load time.
    assert "unless(boundp('RBDataBuf) RBDataBuf = \"\")" in il
    assert "unless(boundp('RBDataBufIpc) RBDataBufIpc = nil)" in il
    # Full SKILL file must have balanced parentheses.
    assert _balanced(il)

    data_handler = il[il.index("procedure(RBIpcDataHandler"):il.index("procedure(RBDispatchLine")]
    dispatch = il[il.index("procedure(RBDispatchLine"):il.index("procedure(RBIpcErrHandler")]
    finish_handler = il[il.index("procedure(RBIpcFinishHandler"):il.index("procedure(RBQuoteShellArg")]
    start_proc = il[il.index("procedure(RBStart()"):il.index("procedure(RBStop()")]

    # RBIpcDataHandler buffers fragments, resets on IPC handle change, splits
    # on LF (char 10) via built-in index(), strips trailing CR (char 13), and
    # advances RBDataBuf before dispatching each complete non-empty line.
    assert "RBDataBufIpc && RBDataBufIpc != ipcId" in data_handler
    assert 'RBDataBuf = ""' in data_handler
    assert "RBDataBufIpc = ipcId" in data_handler
    assert "intToChar(10)" in data_handler
    assert "intToChar(13)" in data_handler
    assert "while(setq(rest index(RBDataBuf nl))" in data_handler
    assert "nindex(" not in il
    assert data_handler.index("RBDataBuf = if(greaterp(restLen 1)") < data_handler.index(
        "RBDispatchLine(ipcId line)"
    )

    # RBDispatchLine preserves vbtag stripping, trailing-comment-safe progn,
    # and STX/NAK framed replies.
    assert 'equal(substring(data 1 6) "vbtag:")' in dispatch
    assert 'evalstring(strcat("(progn " data nl ")"))' in dispatch
    assert "intToChar(2)" in dispatch and "intToChar(21)" in dispatch

    # Both RBStart (zombie reset + fresh launch) and RBIpcFinishHandler reset
    # RBDataBuf so stale fragments cannot bleed across daemon lifetimes.
    assert start_proc.count('RBDataBuf = ""') == 2
    assert start_proc.count("RBDataBufIpc = nil") == 2
    assert "RBDataBufIpc = RBIpc" in start_proc
    assert start_proc.index("RBIpc = ipcBeginProcess(") < start_proc.index("RBDataBufIpc = RBIpc")
    assert 'RBDataBuf = ""' in finish_handler
    assert "RBDataBufIpc = nil" in finish_handler

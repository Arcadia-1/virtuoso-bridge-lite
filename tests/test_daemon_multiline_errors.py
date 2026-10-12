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
from pathlib import Path

from tests.test_recoverable_daemon import _import_daemon, _resources

DAEMON_CONSTANTS = ("_MULTILINE_PREFIX", "_MULTILINE_SUFFIX", "_MULTILINE_RESULT")


def _constants(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in DAEMON_CONSTANTS:
                values[name] = ast.literal_eval(node.value)
    return values


def _balanced(text: str) -> bool:
    depth, in_string, escaped = 0, False, False
    for char in text:
        if in_string:
            escaped = char == "\\" and not escaped
            if char == '"' and not escaped:
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


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
    assert py27 == py3

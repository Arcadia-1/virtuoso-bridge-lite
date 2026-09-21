"""Opt-in tests of the packaged bridge in isolated local Virtuoso sessions.

In a shell configured for Cadence, run:
  VB_RUN_IPC_LOG_TESTS=1 VB_TEST_VIRTUOSO=/path/to/virtuoso \
    pytest tests/test_ipc_logging_live.py

No existing bridge is loaded or restarted. Each case owns a temporary workspace,
port and token. Both SKILL and daemon code come from the package under test.
"""
from __future__ import annotations

from importlib import resources
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

import pytest

from virtuoso_bridge import VirtuosoClient

pytestmark = pytest.mark.skipif(
    os.getenv("VB_RUN_IPC_LOG_TESTS") != "1",
    reason="set VB_RUN_IPC_LOG_TESTS=1 and VB_TEST_VIRTUOSO for isolated live tests",
)


def _wait(predicate, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timed out waiting for Virtuoso test condition")


@pytest.mark.parametrize("enabled,filename", [
    (None, None),
    ("0", "disabled log.log"),
    ("1", "custom log.log"),
    pytest.param("1", "literal 'quote' '$RB_PORT' \"$HOME\" $(printf substituted) `printf substituted` \\ log.log",
                 id="literal-shell-characters"),
    ("1", None),
    ("1", ""),
])
def test_ipc_log_configuration_and_requests(tmp_path, enabled, filename):
    executable = os.environ["VB_TEST_VIRTUOSO"]
    # Exercise spaces in both the directory and the filename.
    work = tmp_path / "Virtuoso workspace"
    work.mkdir()
    resource = resources.files("virtuoso_bridge.virtuoso.basic.resources")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = secrets.token_hex(32)
    token_path = work / "token"
    token_path.write_text(token)
    token_path.chmod(0o600)
    identity = work / "identity.txt"
    logfile = work / (filename or "ramic-bridge.log")
    environment = os.environ.copy()
    environment.pop("RB_LOG_ENABLED", None)
    environment.pop("RB_LOG_PATH", None)
    environment.update(
        RB_PORT=str(port), RB_TOKEN_PATH=str(token_path),
        RB_DAEMON_PATH=str(resource.joinpath("ramic_bridge_daemon_3.py")),
        RB_PYTHON_PATH=sys.executable, RB_IDENTITY_PATH=str(identity),
    )
    if enabled is not None:
        environment["RB_LOG_ENABLED"] = enabled
    if filename is not None:
        environment["RB_LOG_PATH"] = str(logfile) if filename else ""
    startup = work / "startup.il"
    startup.write_text(f'load({json.dumps(str(resource.joinpath("ramic_bridge.il")))})\n')
    client = VirtuosoClient(port=port, timeout=5, daemon_token=token, log_to_ciw=False)

    def evaluate(code):
        result = client.execute_skill(code, timeout=5)
        assert not result.errors, result
        return result.output

    daemon_pids = set()
    with (work / "stdout.log").open("w") as output:
        process = subprocess.Popen(
            [executable, "-nograph", "-nocdsinit", "-restore", str(startup),
             "-log", str(work / "CDS.log")],
            cwd=work, env=environment, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            def ready():
                assert process.poll() is None, (work / "stdout.log").read_text()
                if not identity.exists():
                    return False
                for line in identity.read_text().splitlines():
                    if line.startswith("pid="):
                        daemon_pids.add(int(line[4:]))
                        return True
                return False

            _wait(ready)
            assert evaluate("RBDLog") == ("t" if enabled == "1" else "nil")
            assert evaluate("RBLogPath") == json.dumps(str(logfile))
            assert evaluate("RBMonitor->RBMBolLog->prompt") == json.dumps(f"Daemon log ({logfile})")
            for value in range(100):
                assert evaluate(f"{value}+1") == str(value + 1)
            assert evaluate("nil") == "nil"
            assert evaluate('"IPC_LOG_PROBE"') == '"IPC_LOG_PROBE"'
            failure = client.execute_skill('error("intentional test error")', timeout=5)
            assert failure.errors and "intentional test error" in str(failure.errors)
            assert evaluate("40+2") == "42"

            expected = "log=" + str(logfile) if enabled == "1" else "log=off"
            _wait(lambda: expected in (work / "stdout.log").read_text())
            if enabled == "1":
                assert logfile.exists(), (work / "stdout.log").read_text()
                _wait(lambda: logfile.exists() and "IPC_LOG_PROBE" in logfile.read_text())
            else:
                assert not logfile.exists()

            if enabled == "0":
                # Run the actual monitor callback after the TCP reply has been
                # delivered, as applying this setting restarts its daemon.
                for value in ("t", "nil"):
                    before = identity.read_text()
                    evaluate(f'hiRegTimer("RBMonitor->RBMBolLog->value = {value} RBMApply()" 5)')
                    _wait(lambda: identity.read_text() != before)
                    assert ready()
                    client = VirtuosoClient(port=port, timeout=5,
                                            daemon_token=token, log_to_ciw=False)
                    assert evaluate("RBDLog") == value
                    assert evaluate("6*7") == "42"
                    if value == "t":
                        _wait(lambda: logfile.exists() and "RB-banner" in logfile.read_text())
                size = logfile.stat().st_size
                assert evaluate('"LOG_DISABLED_PROBE"') == '"LOG_DISABLED_PROBE"'
                time.sleep(0.2)
                assert logfile.stat().st_size == size
        finally:
            try:
                evaluate('hiRegTimer("RBStop() exit()" 2)')
                process.wait(timeout=8)
            except Exception:
                pass
            # Cleanup remains restricted to this fixture's process group and
            # identity-recorded daemon PIDs, even if startup/assertions failed.
            for pid in daemon_pids:
                proc = Path("/proc") / str(pid)
                if proc.exists() and (proc / "cwd").resolve() == work:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)

"""Opt-in isolated Virtuoso exit tests; never stop an existing user session.

Use the existing Cadence environment and set VB_RUN_LIFECYCLE_TESTS=1 and
VB_TEST_VIRTUOSO=/path/to/virtuoso. Artifacts use pytest's temporary directory.
"""

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
    os.getenv("VB_RUN_LIFECYCLE_TESTS") != "1" or sys.platform != "linux",
    reason="set VB_RUN_LIFECYCLE_TESTS=1 and VB_TEST_VIRTUOSO for Linux live tests",
)


def _wait(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for session lifecycle")


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except FileNotFoundError:
        return False


@pytest.fixture
def sessions(tmp_path):
    owned = []

    def launch(name, automatic=False):
        work = tmp_path / name
        work.mkdir()
        # Keep any library creation in this private session, not a site cds.lib.
        (work / "cds.lib").write_text("")
        resource = resources.files("virtuoso_bridge.virtuoso.basic.resources")
        skill = str(resource.joinpath("ramic_bridge.il"))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        token = secrets.token_hex(32)
        token_path = work / "token"
        token_path.write_text(token)
        token_path.chmod(0o600)
        identity = work / "identity"
        startup = work / "startup.il"
        startup.write_text(f"load({json.dumps(skill)})\n")
        env = dict(os.environ, RB_PORT=str(port), RB_TOKEN_PATH=str(token_path),
                   RB_IDENTITY_PATH=str(identity), RB_PYTHON_PATH=sys.executable,
                   RB_DAEMON_PATH=str(resource.joinpath("ramic_bridge_daemon_3.py")),
                   RB_LOG_ENABLED="1", RB_LOG_PATH=str(work / "bridge.log"))
        env["RB_AUTO_PORT"] = "1" if automatic else "0"
        if automatic:
            env["RB_PORT"] = "0"
            env["RB_SCRATCH_ROOT"] = str(work / "private")
        output = (work / "stdout.log").open("w")
        proc = subprocess.Popen(
            [os.environ["VB_TEST_VIRTUOSO"], "-nograph", "-nocdsinit", "-restore",
             str(startup), "-log", str(work / "CDS.log")],
            cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        item = dict(proc=proc, output=output, pids=[], work=work)
        owned.append(item)
        if automatic:
            from virtuoso_bridge.virtuoso.basic.resources import ramic_registry, ramic_token
            def records():
                return ramic_registry.discover(root=str(work / "private"))
            _wait(lambda: len(records()) == 1)
            record = records()[0]
            daemon_pid, port = record["daemon_pid"], record["port"]
            token = ramic_token.read_existing()
        else:
            _wait(lambda: identity.exists() and "pid=" in identity.read_text())
            daemon_pid = int(dict(line.split("=", 1) for line in identity.read_text().splitlines())["pid"])
        ipc_pid = int(Path(f"/proc/{daemon_pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        item["pids"].extend([daemon_pid, ipc_pid])
        client = VirtuosoClient(port=port, daemon_token=token, timeout=5, log_to_ciw=False)
        if automatic:
            from types import SimpleNamespace
            client._tunnel = SimpleNamespace(_discover_instances=True, instance_record=record)

            def refresh_after_restart(previous):
                _wait(lambda: len(records()) == 1 and records()[0]["instance"] != previous["instance"])
                fresh = records()[0]
                assert fresh["pid"] == previous["pid"]
                client._port = fresh["port"]
                client._daemon_caps = None
                client._tunnel.instance_record = fresh
                item["pids"].append(fresh["daemon_pid"])
                item["record"] = fresh
                return fresh
            item.update(record=record, refresh=refresh_after_restart, client=client)

        def evaluate(code):
            result = client.execute_skill(code)
            assert not result.errors, result
            return result.output

        virtuoso_pid = int(evaluate("ipcGetPid()"))
        item["pids"].append(virtuoso_pid)
        assert client.remote_virtuoso_pid == virtuoso_pid
        item.update(evaluate=evaluate, virtuoso_pid=virtuoso_pid, daemon_pid=daemon_pid,
                    ipc_pid=ipc_pid, port=port, skill=skill)
        return item

    yield launch
    for item in reversed(owned):
        # Assertion failures still clean only this fixture's recorded PIDs.
        for pid in item["pids"]:
            if _alive(pid) and Path(f"/proc/{pid}/cwd").resolve() == item["work"]:
                os.kill(pid, signal.SIGKILL)
        try:
            os.killpg(item["proc"].pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        item["proc"].wait(timeout=5)
        item["output"].close()


@pytest.mark.parametrize("shutdown", ["exit", "kill", "abort"])
def test_owner_exit_releases_only_its_daemon(sessions, shutdown):
    keeper = sessions("keeper")
    target = sessions("target")
    evaluate = target["evaluate"]
    # Reload does not add an exit hook or replace a healthy daemon.
    assert evaluate(f'load({json.dumps(target["skill"])})') == "t"
    assert int(evaluate("RBLastPid")) == target["daemon_pid"]
    # A cancelled exit must leave the bridge usable.
    evaluate("procedure(RBTestCancelExit() 'ignoreExit)\nregExitBefore('RBTestCancelExit)")
    evaluate("exit()")
    assert evaluate("6*7") == "42"
    evaluate("remExitProc('RBTestCancelExit)")

    if shutdown == "exit":
        evaluate('hiRegTimer("exit()" 2)')
    else:
        os.kill(target["virtuoso_pid"], signal.SIGKILL if shutdown == "kill" else signal.SIGABRT)
    target["proc"].wait(timeout=15)
    _wait(lambda: not _alive(target["daemon_pid"]) and not _alive(target["ipc_pid"]), timeout=3)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", target["port"])) != 0
    assert keeper["evaluate"]("40+2") == "42"
    if shutdown == "exit":
        log = (target["work"] / "stdout.log").read_text()
        assert log.count("] stopping") == 1, log
    # Keeper also exits through the ordinary path, without an explicit RBStop.
    keeper["evaluate"]('hiRegTimer("exit()" 2)')
    keeper["proc"].wait(timeout=15)
    _wait(lambda: not _alive(keeper["daemon_pid"]) and not _alive(keeper["ipc_pid"]), timeout=3)


def test_automatic_reload_banner_and_bounded_restart_preserve_other_ciw(sessions):
    keeper = sessions("automatic-keeper", automatic=True)
    target = sessions("automatic-target", automatic=True)
    evaluate = target["evaluate"]
    original = target["record"]
    assert evaluate(f'load({json.dumps(target["skill"])})') == "t"
    assert int(evaluate("RBPort")) == original["port"]
    assert target["client"]._ensure_daemon_capabilities(time.monotonic() + 5)["build"] == "instance-discovery-2"
    for value in ("", "oops", "-1", "65536", "12oops"):
        banner = "[RB-banner] pid=123 bind=127.0.0.1:" + value + "\n"
        evaluate("RBIpcErrHandler(RBIpc %s)" % json.dumps(banner))
        assert int(evaluate("RBPort")) == original["port"]
    # Schedule once; never replay a request whose daemon may have stopped.
    evaluate('hiRegTimer("RBSetPort(0)" 2)')
    fresh = target["refresh"](original)
    assert not _alive(original["daemon_pid"])
    assert fresh["daemon_pid"] != original["daemon_pid"]
    assert evaluate("6*7") == "42"
    # Exercise the actual timeout and timer branches against a live child.
    evaluate("RBRequestedPort = 0 RBApplyPort(0)")
    assert int(evaluate("RBPort")) == fresh["port"]
    evaluate("RBApplyPort(2)")
    time.sleep(0.5)
    assert int(evaluate("RBPort")) == fresh["port"]
    assert _alive(fresh["daemon_pid"])
    assert evaluate('errset(RBSetPort("invalid") nil)') == "nil"
    assert evaluate('errset(progn(RBMonitor->RBMIntPort->value = -1 RBMApply()) nil)') == "nil"
    evaluate("RBMonitor->RBMIntPort->value = 0")
    evaluate("RBMonitor->RBMBolLog->value = !RBDLog")
    evaluate('hiRegTimer("RBMApply()" 2)')
    monitored = target["refresh"](fresh)
    assert not _alive(fresh["daemon_pid"])
    assert evaluate("6*7") == "42"
    assert keeper["evaluate"]("40+2") == "42"
    assert int(keeper["evaluate"]("RBLastPid")) == keeper["daemon_pid"]
    evaluate('hiRegTimer("RBStopAll()" 2)')
    _wait(lambda: not _alive(monitored["daemon_pid"]))
    assert keeper["evaluate"]("40+2") == "42"

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

    def launch(name, automatic=False, working_dir=None, registry_name=None, resource_dir=None, graphical=False):
        work = tmp_path / name
        work.mkdir()
        cwd = tmp_path / working_dir if working_dir else work
        cwd.mkdir(exist_ok=True)
        # Keep any library creation in this private session, not a site cds.lib.
        (cwd / "cds.lib").write_text("")
        resource = Path(resource_dir) if resource_dir else resources.files("virtuoso_bridge.virtuoso.basic.resources")
        skill = str(resource.joinpath("ramic_bridge.il"))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        token = secrets.token_hex(32)
        legacy_token = token
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
            registry_root = tmp_path / registry_name if registry_name else work / "private"
            env["RB_SCRATCH_ROOT"] = str(registry_root)
            from virtuoso_bridge.virtuoso.basic.resources import ramic_registry
            prior = {r["instance"] for r in ramic_registry.discover(root=str(registry_root))}
        display = None
        if graphical:
            from tests.test_x11_dialog_close_native import Display, XVFB
            if not XVFB:
                pytest.skip("isolated native screenshot requires Xvfb")
            display = Display()
            env["DISPLAY"] = display.name
        output = (work / "stdout.log").open("w")
        proc = subprocess.Popen(
            [os.environ["VB_TEST_VIRTUOSO"], *([] if graphical else ["-nograph"]), "-nocdsinit", "-restore",
             str(startup), "-log", str(work / "CDS.log")],
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        item = dict(proc=proc, output=output, pids=[], work=work, cwd=cwd, display=display)
        owned.append(item)
        if automatic:
            from virtuoso_bridge.virtuoso.basic.resources import ramic_registry
            def records():
                rows = ramic_registry.discover(root=str(registry_root))
                if item.get("virtuoso_pid"):
                    return [r for r in rows if r["pid"] == item["virtuoso_pid"]]
                return [r for r in rows if r["instance"] not in prior]
            _wait(lambda: len(records()) == 1)
            record = records()[0]
            daemon_pid, port = record["daemon_pid"], record["port"]
            token = daemon_auth.read_local_token()
        else:
            _wait(lambda: identity.exists() and "pid=" in identity.read_text())
            daemon_pid = int(dict(line.split("=", 1) for line in identity.read_text().splitlines())["pid"])
        ipc_pid = int(Path(f"/proc/{daemon_pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        item["pids"].extend([daemon_pid, ipc_pid])
        client = VirtuosoClient(port=port, daemon_token=token, timeout=5, log_to_ciw=False)
        if automatic:
            from types import SimpleNamespace
            client._tunnel = SimpleNamespace(_discover_instances=True, instance_record=record, _ssh_runner=None)

            def refresh_after_restart(previous):
                found = []
                def ready():
                    snapshot = records()
                    if len(snapshot) == 1 and snapshot[0]["instance"] != previous["instance"]:
                        found[:] = snapshot
                        return True
                    return False
                _wait(ready)
                fresh = found[0]
                assert fresh["pid"] == previous["pid"]
                client._port = fresh["port"]
                client._daemon_caps = None
                client._tunnel.instance_record = fresh
                item["pids"].append(fresh["daemon_pid"])
                item["record"] = fresh
                return fresh
            item.update(record=record, refresh=refresh_after_restart, registry_root=registry_root)

        def evaluate(code):
            result = client.execute_skill(code)
            assert not result.errors, result
            return result.output

        virtuoso_pid = int(evaluate("ipcGetPid()"))
        item["pids"].append(virtuoso_pid)
        assert client.remote_virtuoso_pid == virtuoso_pid
        item.update(evaluate=evaluate, virtuoso_pid=virtuoso_pid, daemon_pid=daemon_pid,
                    ipc_pid=ipc_pid, port=port, skill=skill, client=client, legacy_token=legacy_token)
        return item

    yield launch
    for item in reversed(owned):
        # Assertion failures still clean only this fixture's recorded PIDs.
        for pid in item["pids"]:
            if _alive(pid) and Path(f"/proc/{pid}/cwd").resolve() == item["cwd"]:
                os.kill(pid, signal.SIGKILL)
        try:
            os.killpg(item["proc"].pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        item["proc"].wait(timeout=5)
        item["output"].close()
        if item["display"] is not None:
            item["display"].close()


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


def test_cli_restart_reconnects_same_ciw_with_same_directory_peer(sessions, tmp_path, monkeypatch):
    from virtuoso_bridge import cli
    from virtuoso_bridge.transport import tunnel
    target = sessions("restart-target", automatic=True, working_dir="shared-project", registry_name="registry")
    keeper = sessions("restart-keeper", automatic=True, working_dir="shared-project", registry_name="registry")
    config = tmp_path / "restart.env"
    config.write_text("VB_REMOTE_HOST=localhost\nVB_REMOTE_SCRATCH_ROOT=" + str(target["registry_root"]) + "\n")
    monkeypatch.setenv("VB_STATE_DIR", str(tmp_path / "client-state"))
    from virtuoso_bridge import env
    monkeypatch.setattr(env, "_RUNTIME_ENV_FILE", config)
    monkeypatch.delenv("VB_REMOTE_PORT", raising=False)
    monkeypatch.setenv("VB_REMOTE_HOST", "localhost")
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", str(target["registry_root"]))
    transport = tunnel.SSHClient.from_env(keep_remote_files=True, read_selection=False)
    original = target["record"]
    transport.connect_instance(original)
    assert cli.main(["restart", "--env", str(config)]) == 0
    fresh_transport = tunnel.SSHClient.from_env(keep_remote_files=True)
    fresh = fresh_transport.instance_record
    target["pids"].append(fresh["daemon_pid"])
    assert fresh["pid"] == original["pid"] and fresh["instance"] != original["instance"]
    assert cli.main(["status", "--env", str(config)]) == 0
    fresh_transport.verify_instance()
    assert keeper["evaluate"]("40+2") == "42"
    assert int(keeper["evaluate"]("RBLastPid")) == keeper["daemon_pid"]
    fresh_transport.stop()


def test_full_upgrade_from_isolated_legacy_fixed_daemon(sessions, tmp_path, monkeypatch):
    legacy_resources = os.getenv("VB_TEST_LEGACY_RESOURCES")
    if not legacy_resources:
        pytest.skip("set VB_TEST_LEGACY_RESOURCES to the pre-discovery resource bundle")
    from virtuoso_bridge import cli
    from virtuoso_bridge import env
    from virtuoso_bridge.transport import tunnel
    from virtuoso_bridge.virtuoso.basic.resources import ramic_registry
    target = sessions("legacy-upgrade", resource_dir=legacy_resources)
    registry_root = target["work"] / "new-registry"
    config = tmp_path / "upgrade.env"
    config.write_text("VB_REMOTE_HOST=localhost\nVB_REMOTE_SCRATCH_ROOT=" + str(registry_root)
                      + "\nVB_REMOTE_PORT=" + str(target["client"].port) + "\n")
    monkeypatch.setattr(env, "_RUNTIME_ENV_FILE", config)
    monkeypatch.setenv("VB_STATE_DIR", str(tmp_path / "upgrade-state"))
    monkeypatch.setenv("VB_REMOTE_HOST", "localhost")
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", str(registry_root))
    monkeypatch.setenv("VB_REMOTE_PORT", str(target["client"].port))
    monkeypatch.setenv("VB_BRIDGE_TOKEN", str(target["work"] / "token"))
    transport = tunnel.SSHClient.from_env(keep_remote_files=True, read_selection=False)
    assert not transport.list_instances()
    original_config = config.read_text()
    assert cli.main(["start", "--env", str(config)]) == 0
    assert cli.main(["status", "--env", str(config)]) == 0
    assert cli.main(["restart", "--env", str(config)]) == 0
    target["pids"].append(int(target["evaluate"]("RBLastPid")))
    assert target["evaluate"]("6*7") == "42"
    # Deliberate staging for a later registered session, not a requirement for
    # connecting to the old endpoint. The old config remains untouched.
    transport.ensure_local_setup()
    setup = transport.existing_setup_path()
    assert setup
    # Operator stop/wait/load, scheduled once in this fixture-owned CIW.
    target["evaluate"]('procedure(VBUpgradeWait(n) '
                       'if(ipcIsAliveProcess(RBIpc) then '
                       'when(n > 0 hiRegTimer(sprintf(nil "VBUpgradeWait(%%d)" n-1) 1)) '
                       'else load(%s)))' % json.dumps(setup))
    target["evaluate"]('hiRegTimer("RBStop() hiRegTimer(\\\"VBUpgradeWait(50)\\\" 1)" 2)')
    def records():
        return ramic_registry.discover(root=str(registry_root))
    _wait(lambda: len(records()) == 1)
    record = records()[0]
    target["pids"].append(record["daemon_pid"])
    assert record["pid"] == target["virtuoso_pid"]
    assert cli.main(["start", "--env", str(config)]) == 0
    assert cli.main(["status", "--env", str(config)]) == 0
    assert config.read_text() == original_config
    connected = tunnel.SSHClient.from_env(keep_remote_files=True)
    connected.verify_instance()
    connected.stop()


def test_automatic_reload_banner_and_bounded_restart_preserve_other_ciw(sessions):
    keeper = sessions("automatic-keeper", automatic=True)
    target = sessions("automatic-target", automatic=True)
    evaluate = target["evaluate"]
    original = target["record"]
    assert evaluate(f'load({json.dumps(target["skill"])})') == "t"
    assert int(evaluate("RBPort")) == original["port"]
    assert target["client"]._ensure_daemon_capabilities(time.monotonic() + 5)["build"] == "instance-discovery-3"
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


def test_fixed_discovery_port_and_explicit_legacy_mode_reset(sessions):
    from virtuoso_bridge.transport.tunnel import _generate_virtuoso_setup_il
    target = sessions("mode-transition", automatic=True)
    evaluate = target["evaluate"]
    original = target["record"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        fixed = probe.getsockname()[1]
    evaluate('hiRegTimer("RBSetPort(%d)" 2)' % fixed)
    pinned = target["refresh"](original)
    assert pinned["port"] == fixed
    assert evaluate('getShellEnvVar("RB_AUTO_PORT")').strip('"') == "1"
    identity = target["work"] / "legacy-identity"
    setup = target["work"] / "legacy-setup.il"
    resource = Path(target["skill"]).parent
    setup.write_text(_generate_virtuoso_setup_il(
        str(resource / "ramic_bridge_daemon_3.py"), target["skill"], sys.executable,
        port=fixed, identity_path=str(identity)))
    evaluate('procedure(VBModeWait(n) if(ipcIsAliveProcess(RBIpc) then '
             'when(n > 0 hiRegTimer(sprintf(nil "VBModeWait(%%d)" n-1) 1)) '
             'else load(%s)))' % json.dumps(str(setup)))
    evaluate('hiRegTimer("RBStop() hiRegTimer(\\\"VBModeWait(50)\\\" 1)" 2)')
    _wait(lambda: identity.exists() and "pid=" in identity.read_text())
    child = int(dict(line.split("=", 1) for line in identity.read_text().splitlines())["pid"])
    target["pids"].append(child)
    legacy = VirtuosoClient(port=fixed, daemon_token=target["legacy_token"], timeout=5)
    caps = legacy._ensure_daemon_capabilities(time.monotonic() + 5)
    assert caps["auth"] == "on" and not caps.get("instance_record")
    result = legacy.execute_skill('getShellEnvVar("RB_AUTO_PORT")', retry_connect=False)
    assert not result.errors and result.output.strip('"') == "0"


def test_local_screenshot_on_fixture_owned_graphical_ciw(sessions, tmp_path, monkeypatch):
    target = sessions("screenshot-capture", automatic=True, graphical=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/screen capture")
    monkeypatch.setattr("virtuoso_bridge.transport.remote_paths.load_vb_env", lambda: None)
    output = tmp_path / "captured.png"
    result = target["client"].screenshot(output)
    assert not result.errors, result
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert list((tmp_path / "screen capture").rglob("*.png"))

"""Discovery selection plus native Linux filesystem/process regressions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from virtuoso_bridge.instance_discovery import InstanceSelectionError, select_instance
from virtuoso_bridge.transport.tunnel import SSHClient
from virtuoso_bridge.virtuoso.basic.resources import ramic_registry as registry


def row(port=65001, pid=10, work_dir="/project/a"):
    return dict(port=port, pid=pid, daemon_pid=pid + 1, uid=1000, user="fixture",
                work_dir=work_dir, instance="%032x" % pid)


def test_selection_requires_unique_instance_and_exact_port():
    a, b = row(), row(65002, 20)
    with pytest.raises(InstanceSelectionError, match="65001  10  /project/a"):
        select_instance([a, b])
    assert select_instance([a, b], port=65002) == b
    with pytest.raises(InstanceSelectionError, match="No valid instance"):
        select_instance([a], port=65002)
    assert select_instance([]) is None


def test_port_cannot_bypass_working_directory():
    a, b = row(), row(65002, 20, "/project/b")
    assert select_instance([a, b], "/project/a/../b") == b
    with pytest.raises(InstanceSelectionError):
        select_instance([a, b], "/project/a", port=b["port"])
    with pytest.raises(InstanceSelectionError, match="absolute"):
        select_instance([a], "relative")


def test_registered_start_cli_exposes_port():
    cli = Path(sys.executable).parent / ("virtuoso-bridge.exe" if os.name == "nt" else "virtuoso-bridge")
    result = subprocess.run([str(cli), "start", "--help"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    assert "--port PORT" in result.stdout


def test_legacy_env_port_selects_and_scopes_saved_state(tmp_path, monkeypatch):
    from virtuoso_bridge import env
    from virtuoso_bridge.transport import tunnel
    config = tmp_path / "bridge.env"
    config.write_text("VB_REMOTE_HOST=localhost\n")
    monkeypatch.setattr(env, "_RUNTIME_ENV_FILE", config)
    monkeypatch.setenv("VB_REMOTE_HOST", "localhost")
    monkeypatch.setenv("VB_REMOTE_PORT", "61234")
    client = SSHClient.from_env()
    assert client._legacy_port == 61234
    assert client._selected_port is None
    first = tunnel._selection_file()
    monkeypatch.setenv("VB_REMOTE_PORT", "61235")
    assert tunnel._selection_file() != first
    monkeypatch.setenv("VB_REMOTE_PORT", "0")
    with pytest.raises(ValueError, match="between 1 and 65535"):
        SSHClient.from_env()


def test_cli_port_overrides_env_port(monkeypatch):
    from virtuoso_bridge import cli
    from virtuoso_bridge.instance_discovery import InstanceSelectionError
    monkeypatch.setenv("VB_REMOTE_HOST", "fixture-host")
    client = SSHClient(remote_host="fixture-host", selected_port=61234)
    def warm():
        raise InstanceSelectionError("fixture stop")
    def from_env(**kwargs):
        assert kwargs["selected_port"] == 61235
        return client
    monkeypatch.setattr(SSHClient, "from_env", from_env)
    monkeypatch.setattr(client, "warm", warm)
    monkeypatch.setattr(client, "close", lambda: None)
    assert cli._start_one_profile(None, 61235) == 1


def test_missing_selected_port_never_writes_allocation_request(monkeypatch):
    import shlex
    from virtuoso_bridge.transport.ssh import CommandResult
    from virtuoso_bridge.transport import tunnel
    client = SSHClient(remote_host="fixture-host", discover_instances=True,
                       selected_port=61234, expected_work_dir="/project with spaces",
                       shared_setup="/opt/bridge/virtuoso_setup.il")
    commands = []
    class Runner:
        def run_command(self, command, **kwargs):
            commands.append(shlex.split(command))
            return CommandResult(0, "[]", "")
    client._ssh_runner = Runner()
    monkeypatch.setattr(client, "_detect_remote_python", lambda *args, **kwargs: ("python3", 3, 9))
    monkeypatch.setattr(tunnel, "instance_scratch_root", lambda *args, **kw: "/scratch with spaces")
    with pytest.raises(InstanceSelectionError, match="VB_REMOTE_PORT 61234"):
        client.discover_instance()
    assert commands[0][3:] == ["--scratch-root", "/scratch with spaces", "--read-only"]


def test_signed_endpoint_must_match_selected_record():
    from types import SimpleNamespace
    from tests.test_daemon_auth import _AuthDaemon, TOKEN
    from virtuoso_bridge.daemon_auth import DaemonAuthError
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
    selected = row()
    caps = dict(proto=1, auth="on", daemon="ramic-bridge", virtuoso_pid=10,
                instance_record=row(pid=20))
    daemon = _AuthDaemon(caps_body=json.dumps(caps))
    try:
        client = VirtuosoClient(host="127.0.0.1", port=daemon.port, daemon_token=TOKEN,
                                tunnel=SimpleNamespace(instance_record=selected))
        with pytest.raises(DaemonAuthError, match="does not match"):
            client._ensure_daemon_capabilities(time.monotonic() + 3)
        assert not daemon.executed
    finally:
        daemon.close()


def test_cached_capabilities_cannot_outlive_instance_selection(monkeypatch):
    from types import SimpleNamespace
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
    tunnel = SimpleNamespace(_discover_instances=True, instance_record=row(65002, 20))
    client = VirtuosoClient(tunnel=tunnel, daemon_token="ab" * 32)
    client._daemon_caps = dict(instance_record=row(), auth="on")
    def exchange(*args):
        raise ConnectionRefusedError("fresh handshake required")
    monkeypatch.setattr(client, "_exchange_payload", exchange)
    with pytest.raises(ConnectionRefusedError, match="fresh handshake"):
        client._ensure_daemon_capabilities(time.monotonic() + 1)


def test_python27_automatic_credential_is_an_hmac_byte_key(monkeypatch):
    import ast
    import hashlib
    import hmac
    from types import SimpleNamespace
    source = (Path(registry.__file__).parent / "ramic_bridge_daemon_27.py").read_text(encoding="utf-8")
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_load_or_create_token")
    # Exercise the actual function's automatic branch independently of the
    # Python-2-only IPC code. This does not replace a native Python 2 run.
    module = ast.Module(body=[function], type_ignores=[])
    scope = dict(os=SimpleNamespace(environ={"RB_AUTO_PORT": "1"}), sys=sys)
    monkeypatch.setitem(sys.modules, "ramic_token", SimpleNamespace(read_or_create=lambda: "ab" * 32))
    exec(compile(module, "daemon27-token", "exec"), scope)
    key = scope["_load_or_create_token"]()
    assert hmac.new(key, b"request", hashlib.sha256).digest() == hmac.new(
        b"ab" * 32, b"request", hashlib.sha256).digest()


def test_saved_selection_is_scoped_to_config_and_expected_directory(tmp_path, monkeypatch):
    from virtuoso_bridge.transport import tunnel
    from virtuoso_bridge import env
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    first.write_text("VB_REMOTE_HOST=localhost\n")
    second.write_text("VB_REMOTE_HOST=localhost\n")
    monkeypatch.setattr(tunnel, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(env, "_RUNTIME_ENV_FILE", first)
    monkeypatch.setattr(tunnel, "load_vb_env", lambda **kwargs: None)
    for key in list(os.environ):
        if key.startswith("VB_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("VB_REMOTE_HOST", "localhost")
    monkeypatch.setenv("VB_REMOTE_WORK_DIR", "/project/a")
    client = SSHClient.from_env()
    client._port = 65099
    client.instance_record = row(65099)
    client.save_state()
    assert SSHClient.read_state()["remote_port"] == 65099
    assert SSHClient.from_env().instance_record == row(65099)
    monkeypatch.setattr(env, "_RUNTIME_ENV_FILE", second)
    assert not tunnel._read_selection()
    monkeypatch.setattr(env, "_RUNTIME_ENV_FILE", first)
    monkeypatch.setenv("VB_REMOTE_WORK_DIR", "/project/b")
    assert not tunnel._read_selection()


def test_automatic_setup_contains_no_fixed_identity_or_port():
    from virtuoso_bridge.transport.tunnel import _generate_virtuoso_setup_il
    source = _generate_virtuoso_setup_il("/opt/bridge/daemon.py", "/opt/bridge/ramic_bridge.il", port=0)
    assert 'setShellEnvVar("RB_PORT" "0")' in source
    assert 'setShellEnvVar("RB_AUTO_PORT" "1")' in source
    assert 'setShellEnvVar("RB_IDENTITY_PATH" "")' in source
    source = _generate_virtuoso_setup_il("/opt/daemon.py", "/opt/bridge.il", port=0,
                                         scratch_root="/private/scratch")
    assert 'setShellEnvVar("RB_SCRATCH_ROOT" "/private/scratch")' in source


def test_bootstrap_port_switch_is_bounded_and_balanced():
    from tests.test_daemon_multiline_errors import _balanced
    source = (Path(registry.__file__).parent / "ramic_bridge.il").read_text(encoding="utf-8")
    # Strip comments for the small structural check; native SKILL verification
    # remains distinct from this source-level regression.
    start = source.index("procedure(RBSetPort(")
    end = source.index("procedure(RBStopAll()", start)
    code = source[start:end]
    assert _balanced(code)
    assert "RBApplyPort(50" in code
    assert "attempts > 0" in code
    assert code.index("RBStop()") < code.index('hiRegTimer(')
    depth = 0
    in_string = escaped = comment = False
    for char in source:
        if comment:
            comment = char != "\n"
        elif in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == ";":
            comment = True
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            assert depth >= 0
    assert depth == 0 and not in_string


def test_discovery_does_not_deploy_or_write_port_config(monkeypatch):
    from virtuoso_bridge.transport.ssh import CommandResult
    client = SSHClient(remote_host="host", remote_user="test-user", discover_instances=True)
    class Runner:
        _vb_remote_home = "/home/fixture"
        def run_command(self, command, **kwargs):
            return CommandResult(0, json.dumps([row()]), "")
    client._ssh_runner = Runner()
    monkeypatch.setattr(client, "_detect_remote_python", lambda *args, **kwargs: ("python3", 3, 9))
    monkeypatch.setattr(client, "refresh_daemon_token", lambda: "ab" * 32)
    monkeypatch.setattr(client, "ensure_remote_setup", lambda: pytest.fail("unnecessary deployment"))
    client.discover_instance()
    assert client.remote_port == 65001
    assert client.instance_record == row()


def test_ambiguous_start_prints_all_rows_without_ssh_diagnosis(monkeypatch, capsys):
    from virtuoso_bridge import cli
    monkeypatch.setenv("VB_REMOTE_HOST", "test-host")
    client = SSHClient(remote_host="test-host")
    monkeypatch.setattr(SSHClient, "from_env", lambda **kwargs: client)
    def warm():
        select_instance([row(), row(65002, 20)])
    monkeypatch.setattr(client, "warm", warm)
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(cli, "_format_ssh_failure", lambda *args: pytest.fail("not an SSH failure"))
    assert cli._start_one_profile(None) == 1
    out = capsys.readouterr().out
    assert "65001  10" in out and "65002  20" in out and "--port" in out


def test_start_reports_daemon_reset_without_misdiagnosing_ssh(monkeypatch, capsys):
    from virtuoso_bridge import cli
    from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
    from virtuoso_bridge.instance_discovery import InstanceConnectionError
    monkeypatch.setenv("VB_REMOTE_HOST", "test-host")
    client = SSHClient(remote_host="test-host", port=34713, local_port=65083)
    client.instance_record = row(34713)
    monkeypatch.setattr(SSHClient, "from_env", lambda **kwargs: client)
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(client, "warm", lambda: client._verify_discovered_instance(1))
    def reset(*args):
        raise ConnectionResetError("connection reset by peer")
    monkeypatch.setattr(VirtuosoClient, "_ensure_daemon_capabilities", reset)
    monkeypatch.setattr(cli, "_format_ssh_failure", lambda *args: pytest.fail("SSH already succeeded"))
    with pytest.raises(InstanceConnectionError) as caught:
        client._verify_discovered_instance(1)
    assert isinstance(caught.value.__cause__, ConnectionResetError)
    assert cli._start_one_profile(None) == 1
    out = capsys.readouterr().out
    assert "Daemon handshake failed" in out
    assert "65083" in out and "34713" in out and "connection reset" in out


linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires native Linux flock and UID semantics")
RESOURCES = Path(registry.__file__).parent
WORKER = '''
import json, socket, sys
sys.path.insert(0, sys.argv[1])
from ramic_registry import bind_instance
s = socket.socket()
lease = bind_instance(s, '127.0.0.1', directory=sys.argv[2], work_dir='/test/project')
print(json.dumps(lease.metadata), flush=True)
sys.stdin.readline()
lease.close()
s.close()
'''


def spawn(directory):
    return subprocess.Popen([sys.executable, "-u", "-c", WORKER, str(RESOURCES), str(directory)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


@linux
def test_concurrent_allocation_keeps_live_leases_and_reclaims_killed_workers(tmp_path):
    directory = tmp_path / "runs"
    processes = [spawn(directory) for _ in range(8)]
    try:
        # Bounded output collection: no readline that could hang the suite.
        deadline = time.monotonic() + 10
        rows = []
        while time.monotonic() < deadline:
            rows = registry.discover(str(directory))
            if len(rows) == len(processes):
                break
            time.sleep(.05)
        assert len(rows) == 8
        assert len({r["port"] for r in rows}) == 8
        assert directory.stat().st_mode & 0o777 == 0o700
        assert all((directory / (r["instance"] + ".json")).stat().st_mode & 0o777 == 0o600 for r in rows)
        sweep = registry.Registry(str(directory))
        try:
            sweep.sweep()
        finally:
            sweep.close()
        assert len(registry.discover(str(directory))) == 8
        stop(processes[0])
        assert len(registry.discover(str(directory))) == 7
        replacement = spawn(directory)
        processes.append(replacement)
        deadline = time.monotonic() + 5
        while len(registry.discover(str(directory))) != 8 and time.monotonic() < deadline:
            time.sleep(.05)
        assert len(list(directory.iterdir())) == 8
        # Graceful exit removes its file, even while other processes are live.
        replacement.communicate("exit\n", timeout=5)
        assert len(list(directory.iterdir())) == 7
    finally:
        for process in processes:
            stop(process)


@linux
def test_live_record_locks_do_not_block_discovery(tmp_path):
    directory = str(tmp_path / "runs")
    with socket.socket() as listener:
        lease = registry.bind_instance(listener, "127.0.0.1", directory=directory)
        try:
            started = time.monotonic()
            for _ in range(20):
                assert registry.discover(directory) == [lease.metadata]
            assert time.monotonic() - started < 2
        finally:
            lease.close()
    assert registry.discover(directory) == []


@linux
def test_occupied_socket_symlink_fifo_and_unlocked_stale_file(tmp_path):
    directory = str(tmp_path / "runs")
    registry.Registry(directory).close()
    target = tmp_path / "untouched"
    target.write_text("keep")
    (Path(directory) / ("1" * 32 + ".json")).symlink_to(target)
    os.mkfifo(str(Path(directory) / ("2" * 32 + ".json")))
    (Path(directory) / ("3" * 32 + ".json")).write_text("stale")
    with socket.socket() as occupied, socket.socket() as candidate:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        with pytest.raises(OSError):
            registry.bind_instance(candidate, "127.0.0.1", occupied.getsockname()[1], directory)
    assert target.read_text() == "keep"
    assert not (Path(directory) / ("3" * 32 + ".json")).exists()
    assert registry.discover(directory) == []


@linux
def test_token_creation_race_converges_without_logging_secret(tmp_path):
    path = tmp_path / "private" / "bridge_token"
    script = "import sys; sys.path.insert(0, sys.argv[1]); from ramic_token import read_or_create; print(read_or_create(sys.argv[2]))"
    processes = [subprocess.Popen([sys.executable, "-c", script, str(RESOURCES), str(path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
    try:
        results = [p.communicate(timeout=10) for p in processes]
        assert all(p.returncode == 0 for p in processes), results
        assert len({out.strip() for out, err in results}) == 1
        assert all(not err for out, err in results)
        assert len(results[0][0].strip()) == 64
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        for process in processes:
            stop(process)


@linux
def test_credential_symlink_is_rejected_without_modifying_target(tmp_path):
    from virtuoso_bridge.virtuoso.basic.resources.ramic_token import read_or_create
    directory = tmp_path / "private"
    directory.mkdir()
    target = tmp_path / "victim"
    target.write_text("ab" * 32)
    (directory / "bridge_token").symlink_to(target)
    with pytest.raises(OSError):
        read_or_create(str(directory / "bridge_token"))
    assert target.read_text() == "ab" * 32


def test_status_discovery_does_not_write_fixed_port_requests(monkeypatch):
    from virtuoso_bridge.transport.ssh import CommandResult
    client = SSHClient(remote_host="host", selected_port=61234)
    monkeypatch.setattr(client, "_instance_root", lambda **kw: "/private/fixture")
    commands = []
    class Runner:
        def run_command(self, command, **kwargs):
            commands.append(command)
            return CommandResult(0, "[]", "")
    client._ssh_runner = Runner()
    monkeypatch.setattr(client, "_detect_remote_python", lambda *a, **kw: ("python3", 3, 9))
    assert client.list_instances() == []
    import shlex
    args = shlex.split(commands[0])[3:]
    assert "--configure" not in args and "--port" not in args

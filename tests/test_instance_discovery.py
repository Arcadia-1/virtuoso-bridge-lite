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


@pytest.mark.parametrize("mode,owner,links", [(0o644, 1000, 1), (0o666, 1000, 1), (0o600, -1, 1), (0o600, 1000, 2)])
def test_record_read_rejects_unsafe_files(tmp_path, monkeypatch, mode, owner, links):
    import stat
    from types import SimpleNamespace
    expected = row()
    payload = json.dumps(expected).encode("utf-8")
    path = tmp_path / "record.json"
    path.write_bytes(payload)
    # Simulate foreign ownership and permissive POSIX modes on every platform;
    # the native harness separately checks real chmod/locks on Linux.
    metadata = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_size=len(payload), st_uid=owner, st_nlink=links)
    monkeypatch.setattr(registry, "os", SimpleNamespace(fstat=lambda fd: metadata, read=os.read, geteuid=lambda: 1000))
    with path.open("rb") as stream:
        with pytest.raises(ValueError, match="metadata"):
            registry._read_record(stream.fileno())


def test_registry_root_uses_daemon_account_without_changing_workflow_defaults(monkeypatch):
    from types import SimpleNamespace
    from virtuoso_bridge.transport import remote_paths
    monkeypatch.setattr(remote_paths, "load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "/tmp")
    runner = SimpleNamespace(user="ssh-alias", run_command=lambda command:
                             SimpleNamespace(returncode=0, stdout="actual-account\n"))
    assert remote_paths.instance_scratch_root(runner) == "/tmp/virtuoso_bridge_actual-account"
    assert remote_paths.default_virtuoso_bridge_dir("ssh-alias", "workflow") == (
        "/tmp/virtuoso_bridge_ssh-alias/workflow")


def test_registry_home_expansion_does_not_change_literal_workflow_root(monkeypatch):
    from types import SimpleNamespace
    from virtuoso_bridge.transport import remote_paths
    monkeypatch.setattr(remote_paths, "load_vb_env", lambda: None)
    monkeypatch.setenv("VB_REMOTE_SCRATCH_ROOT", "~/scratch")
    runner = SimpleNamespace(run_command=lambda command:
                             SimpleNamespace(returncode=0, stdout="/home/remote\n"))
    assert remote_paths.instance_scratch_root(runner) == "/home/remote/scratch"
    assert remote_paths.default_virtuoso_bridge_dir("user", "workflow") == (
        "~/scratch/virtuoso_bridge_user/workflow")


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
    monkeypatch.setenv("VB_AUTO_SESSIONS", "1")
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
    (Path(directory) / ("3" * 32 + ".json")).chmod(0o600)
    with socket.socket() as occupied, socket.socket() as candidate:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        with pytest.raises(OSError):
            registry.bind_instance(candidate, "127.0.0.1", occupied.getsockname()[1], directory)
    assert target.read_text() == "keep"
    assert not (Path(directory) / ("3" * 32 + ".json")).exists()
    assert registry.discover(directory) == []


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


def test_start_reports_unreconcilable_state_without_misdiagnosing_ssh(monkeypatch, capsys):
    from virtuoso_bridge import cli
    from virtuoso_bridge.transport.tunnel import RetainedTunnelStateError
    monkeypatch.setenv("VB_REMOTE_HOST", "test-host")
    client = SSHClient(remote_host="test-host")
    monkeypatch.setattr(SSHClient, "from_env", lambda **kwargs: client)
    monkeypatch.setattr(client, "close", lambda: None)
    def warm():
        raise RetainedTunnelStateError("Cannot reconcile retained tunnel state at '/state.json'")
    monkeypatch.setattr(client, "warm", warm)
    monkeypatch.setattr(cli, "_format_ssh_failure", lambda *args: pytest.fail("not an SSH failure"))
    assert cli._start_one_profile(None) == 1
    out = capsys.readouterr().out
    assert "Cannot reconcile retained tunnel state at '/state.json'" in out
    assert "status --inspect-state" in out and "SSH" not in out


def test_ramic_paths_check_private_allows_setgid_and_rejects_unsafe_modes(monkeypatch):
    import stat
    from types import SimpleNamespace
    from virtuoso_bridge.virtuoso.basic.resources import ramic_paths

    monkeypatch.setattr(ramic_paths.os, "geteuid", lambda: 1000, raising=False)
    for mode in (0o0700, 0o2700):
        info = SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=1000)
        ramic_paths.check_private(info, "/proj/private")

    for mode, uid in (
        (0o4700, 1000),  # setuid rejected
        (0o6700, 1000),  # setuid + setgid rejected
        (0o2750, 1000),  # group bits rejected
        (0o2710, 1000),  # group exec rejected
        (0o2701, 1000),  # other exec rejected
        (0o2500, 1000),  # missing owner write rejected
        (0o2700, 1001),  # foreign owner rejected
    ):
        info = SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=uid)
        with pytest.raises(ValueError, match="mode 0700"):
            ramic_paths.check_private(info, "/proj/private")

    reg_info = SimpleNamespace(st_mode=stat.S_IFREG | 0o2700, st_uid=1000)
    with pytest.raises(ValueError, match="mode 0700"):
        ramic_paths.check_private(reg_info, "/proj/private")


@pytest.mark.skipif(os.name != "posix", reason="POSIX open flags and fcntl")
def test_registry_nfs_safe_locking_and_eacces_fallback(tmp_path, monkeypatch):
    import errno as errno_mod
    import fcntl
    import pwd

    real_registry = registry.Registry

    # Anchor Registry to tmp_path without needing Linux /proc/self/fd.
    class FakeRegistry:
        def __init__(self, path=None, create=True, root=None, legacy=False):
            if legacy:
                raise OSError(errno_mod.ENOENT, "no legacy dir")
            self.anchor = str(tmp_path)

        def files(self, temporaries=False):
            return real_registry.files(self, temporaries=temporaries)

        def sweep(self):
            return real_registry.sweep(self)

        def close(self):
            pass

    monkeypatch.setattr(registry, "Registry", FakeRegistry)
    uid = os.geteuid()
    user = pwd.getpwuid(uid).pw_name

    live_id = "1" * 32
    stale_id = "2" * 32
    live_path = tmp_path / f"{live_id}.json"
    stale_path = tmp_path / f"{stale_id}.json"
    live_meta = dict(
        port=65111, pid=1234, daemon_pid=1235, uid=uid, user=user,
        work_dir="/proj/a", instance=live_id,
    )
    live_path.write_bytes(json.dumps(live_meta).encode("utf-8"))
    live_path.chmod(0o600)
    stale_path.write_bytes(b"stale")
    stale_path.chmod(0o600)

    fd_flags = {}
    fd_paths = {}
    lock_calls = []
    real_open = os.open
    real_close = os.close

    def tracked_open(path, flags, *args):
        fd = real_open(path, flags, *args)
        fd_flags[fd] = flags
        fd_paths[fd] = path
        return fd

    def tracked_close(fd):
        fd_flags.pop(fd, None)
        fd_paths.pop(fd, None)
        return real_close(fd)

    def nfs_flock(fd, op):
        acc = fd_flags[fd] & os.O_ACCMODE
        lock_calls.append((os.path.basename(fd_paths[fd]), acc, op))
        # Simulate Linux NFS: LOCK_EX on O_RDONLY raises EBADF.
        if op == fcntl.LOCK_EX and acc == os.O_RDONLY:
            raise OSError(errno_mod.EBADF, "Bad file descriptor")
        if os.path.basename(fd_paths[fd]) == f"{live_id}.json":
            raise IOError(errno_mod.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(registry.os, "open", tracked_open)
    monkeypatch.setattr(registry.os, "close", tracked_close)
    monkeypatch.setattr(registry, "_flock", nfs_flock)

    # 1. Read-only discovery probes with LOCK_SH on O_RDONLY; keeps stale file.
    rows = registry.discover(directory=str(tmp_path), read_only=True)
    assert rows == [live_meta]
    assert stale_path.exists()
    assert lock_calls
    assert all(acc == os.O_RDONLY and op == fcntl.LOCK_SH for _, acc, op in lock_calls)

    # 2. Sweep opens O_RDWR before LOCK_EX; unlinks stale file, keeps live file.
    lock_calls.clear()
    FakeRegistry().sweep()
    assert not stale_path.exists()
    assert live_path.exists()
    assert lock_calls
    assert all(acc == os.O_RDWR and op == fcntl.LOCK_EX for _, acc, op in lock_calls)

    # 3. Read-write discovery opens O_RDWR before LOCK_EX and removes newly stale records.
    stale_path.write_bytes(b"stale-again")
    stale_path.chmod(0o600)
    lock_calls.clear()
    rows = registry.discover(directory=str(tmp_path), read_only=False)
    assert rows == [live_meta]
    assert not stale_path.exists()
    assert lock_calls
    assert all(acc == os.O_RDWR and op == fcntl.LOCK_EX for _, acc, op in lock_calls)

    # 4. EACCES and EROFS on O_RDWR fall back cleanly to O_RDONLY + LOCK_SH without unlinking.
    for open_errno in (errno_mod.EACCES, errno_mod.EROFS):
        stale_path.write_bytes(b"ro-stale")
        stale_path.chmod(0o600)

        def ro_open(path, flags, *args, _err=open_errno):
            if (flags & os.O_ACCMODE) == os.O_RDWR:
                raise OSError(_err, "open denied")
            return tracked_open(path, flags, *args)

        monkeypatch.setattr(registry.os, "open", ro_open)
        lock_calls.clear()
        rows = registry.discover(directory=str(tmp_path), read_only=False)
        assert rows == [live_meta]
        assert stale_path.exists()
        assert lock_calls
        assert all(acc == os.O_RDONLY and op == fcntl.LOCK_SH for _, acc, op in lock_calls)


@pytest.mark.skipif(os.name != "posix", reason="POSIX pwd and directory descriptors")
def test_registry_decodes_utf8_bytes_for_non_ascii_work_dir_and_user(tmp_path, monkeypatch):
    import errno as errno_mod
    import pwd
    from types import SimpleNamespace

    real_registry = registry.Registry

    # Exercise bytes inputs (as Python 2.7 str provides from os.getcwd()/env/pwd).
    work_dir_text = "/proj/caf\u00e9_\u4e2d"
    work_dir_bytes = work_dir_text.encode("utf-8")
    assert registry._scope(work_dir_bytes) == work_dir_text
    assert registry._request_name(work_dir_bytes) == registry._request_name(work_dir_text)

    class FakeRegistry:
        def __init__(self, path=None, create=True, root=None, legacy=False):
            if legacy:
                raise OSError(errno_mod.ENOENT, "no legacy dir")
            self.fd = os.open(str(tmp_path), os.O_RDONLY)
            self.anchor = str(tmp_path)

        def files(self, temporaries=False):
            return real_registry.files(self, temporaries=temporaries)

        def sweep(self):
            pass

        def close(self):
            os.close(self.fd)

    class FakeSock:
        def bind(self, addr):
            self.addr = addr

        def listen(self, backlog):
            pass

        def getsockname(self):
            return ("127.0.0.1", self.addr[1] or 65222)

    monkeypatch.setattr(registry, "Registry", FakeRegistry)
    monkeypatch.setattr(registry, "_flock", lambda fd, op: None)
    monkeypatch.setattr(
        pwd, "getpwuid",
        lambda _uid: SimpleNamespace(pw_name="user_\u00e9".encode("utf-8")),
    )

    registry.configure_port(65222, work_dir_bytes, directory=str(tmp_path))
    reg = FakeRegistry()
    try:
        assert registry.requested_port(reg, work_dir_bytes) == 65222
    finally:
        reg.close()

    sock = FakeSock()
    lease = registry.bind_instance(sock, "127.0.0.1", directory=str(tmp_path), work_dir=work_dir_bytes)
    try:
        assert lease.metadata["port"] == 65222
        assert lease.metadata["work_dir"] == work_dir_text
        assert lease.metadata["user"] == "user_\u00e9"
        # Discovery also matches decoded non-ASCII pw_name and work_dir.
        monkeypatch.setattr(
            registry, "_flock",
            lambda fd, op: (_ for _ in ()).throw(IOError(errno_mod.EAGAIN, "locked")),
        )
        rows = registry.discover(directory=str(tmp_path), read_only=True)
        assert rows == [lease.metadata]
    finally:
        # Lease.close uses /proc/self/fd on Linux; clean up directly on non-Linux.
        os.close(lease.directory_fd)
        os.close(lease.fd)
        lease.fd = None

"""Standalone Linux acceptance checks; stdlib only, no Cadence or SSH needed.

Usage: python3 native_instance_registry.py /path/to/resources
All test files/credentials are synthetic and private temporary data.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

if not sys.platform.startswith("linux"):
    print("native_instance_registry: skipped; requires Linux flock, UID and /proc/self/fd")
    raise SystemExit(0)

RESOURCES = Path(sys.argv.pop(1)).resolve()
WORKER_PYTHON = os.environ.get("VB_TEST_WORKER_PYTHON", sys.executable)
sys.path.insert(0, str(RESOURCES))
import ramic_registry as registry

WORKER = '''
import json, socket, sys
sys.path.insert(0, sys.argv[1])
from ramic_registry import bind_instance
s = socket.socket()
lease = bind_instance(s, '127.0.0.1', directory=sys.argv[2], work_dir='/synthetic/project')
sys.stdout.write(json.dumps(lease.metadata) + '\\n')
sys.stdout.flush()
sys.stdin.readline()
lease.close()
s.close()
'''


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vb-native-")
        self.root = Path(self.temp.name)
        self.directory = str(self.root / "runs")
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.temp.cleanup()

    def spawn(self, source, *args):
        process = subprocess.Popen([WORKER_PYTHON, "-u", "-c", source, str(RESOURCES), *map(str, args)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def await_rows(self, count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = registry.discover(self.directory)
            if len(rows) == count:
                return rows
            time.sleep(.03)
        self.fail("Timed out discovering %d instances" % count)

    def test_stock_scratch_default_and_flat_records(self):
        source = '''
import os, pwd, sys
sys.path.insert(0, sys.argv[1])
import ramic_registry as r
for key in ('RB_SCRATCH_ROOT', 'VB_REMOTE_SCRATCH_ROOT'):
    os.environ.pop(key, None)
class Account(object):
    pw_dir = sys.argv[2]
    pw_name = 'fixture'
pwd.getpwuid = lambda uid: Account()
assert r.scratch_root() == '/tmp/virtuoso_bridge_fixture'
expected = os.path.join(sys.argv[2], 'private')
os.environ['RB_SCRATCH_ROOT'] = expected
reg = r.Registry()
reg.close()
assert os.path.isdir(expected + '/runs')
assert os.stat(expected).st_mode & 0o777 == 0o700
assert os.listdir(expected) == ['runs']
'''
        worker = self.spawn(source, self.root)
        stdout, stderr = worker.communicate(timeout=5)
        self.assertEqual(worker.returncode, 0, stderr)

    def test_bundle_publisher_with_actual_worker(self):
        source = '''
import os, sys
sys.path.insert(0, sys.argv[1])
from ramic_deploy import publish
destination = sys.argv[2] + '/daemon'
first = {'setup.il': 'loopback', 'daemon.py': 'old'}
assert not publish(destination, first)['reused']
assert publish(destination, first)['reused']
with open(destination + '/json.py', 'w') as stream:
    stream.write('raise RuntimeError("unexpected shadow")')
try:
    publish(destination, first)
except RuntimeError:
    pass
else:
    raise AssertionError('unexpected import member accepted')
try:
    publish(destination, {'setup.il': 'all', 'daemon.py': 'new'})
except RuntimeError:
    pass
else:
    raise AssertionError('differing bundle replaced without authorization')
changed = publish(destination, {'setup.il': 'all', 'daemon.py': 'new'}, replace=True)
assert open(changed['backup'] + '/setup.il').read() == 'loopback'
assert open(destination + '/setup.il').read() == 'all'
assert not os.path.exists(destination + '/json.py')
assert os.path.exists(changed['backup'] + '/json.py')
'''
        worker = self.spawn(source, self.root)
        stdout, stderr = worker.communicate(timeout=5)
        self.assertEqual(worker.returncode, 0, stderr)

    def test_abandoned_temporary_cleanup_preserves_live_writers(self):
        import fcntl
        opened = registry.Registry(self.directory)
        try:
            for prefix in (".record-", ".request-"):
                old = Path(self.directory) / (prefix + "a" * 32)
                old.write_text("incomplete")
                old.chmod(0o600)
                os.utime(old, (time.time() - 600, time.time() - 600))
                live = Path(self.directory) / (prefix + "b" * 32)
                with live.open("w") as stream:
                    live.chmod(0o600)
                    fcntl.flock(stream, fcntl.LOCK_EX)
                    os.utime(live, (time.time() - 600, time.time() - 600))
                    opened.sweep()
                    self.assertFalse(old.exists())
                    self.assertTrue(live.exists())
                opened.sweep()
                self.assertFalse(live.exists())
        finally:
            opened.close()

    def test_distinct_projects_have_distinct_live_records(self):
        self.spawn(WORKER.replace('/synthetic/project', '/project/a'), self.directory)
        self.spawn(WORKER.replace('/synthetic/project', '/project/b'), self.directory)
        rows = self.await_rows(2)
        self.assertEqual(set(row['work_dir'] for row in rows), {'/project/a', '/project/b'})
        self.assertEqual(len(set(row['port'] for row in rows)), 2)

    def test_deployed_nested_layout_remains_discoverable_during_upgrade(self):
        import pwd
        import re
        username = re.sub(r"[^a-zA-Z0-9._-]", "_", pwd.getpwuid(os.geteuid()).pw_name)[:64]
        parent = self.root / ("virtuoso_bridge_" + username)
        parent.mkdir(mode=0o700)
        old = self.spawn(WORKER, parent / "runs")
        new = self.spawn(WORKER, self.root / "runs")
        old_row = json.loads(old.stdout.readline())
        new_row = json.loads(new.stdout.readline())
        rows = registry.discover(root=str(self.root))
        self.assertEqual({row["instance"] for row in rows}, {old_row["instance"], new_row["instance"]})

    def test_registry_rejects_shared_roots_and_symlinks(self):
        shared = self.root / "shared"
        shared.mkdir(mode=0o777)
        shared.chmod(0o777)
        with self.assertRaises(ValueError):
            registry.Registry(root=str(shared))
        self.assertEqual(shared.stat().st_mode & 0o777, 0o777)
        self.assertEqual(list(shared.iterdir()), [])
        link = self.root / "link"
        link.symlink_to(shared)
        with self.assertRaises(OSError):
            registry.Registry(root=str(link))

    def test_registry_ignores_and_preserves_records_with_unsafe_modes(self):
        worker = self.spawn(WORKER, self.directory)
        row = self.await_rows(1)[0]
        path = Path(self.directory) / (row["instance"] + ".json")
        for mode in (0o644, 0o666):
            path.chmod(mode)
            self.assertEqual(registry.discover(self.directory), [])
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        worker.communicate(b"exit\n", timeout=5)
        stale = Path(self.directory) / ("a" * 32 + ".json")
        stale.write_text("stale")
        stale.chmod(0o666)
        self.assertEqual(registry.discover(self.directory), [])
        self.assertTrue(stale.exists())

    def test_concurrent_start_kill_sweep_and_graceful_exit(self):
        workers = [self.spawn(WORKER, self.directory) for _ in range(12)]
        rows = self.await_rows(12)
        self.assertEqual(len({r["port"] for r in rows}), 12)
        self.assertEqual(os.stat(self.directory).st_mode & 0o777, 0o700)
        for record in rows:
            self.assertEqual((Path(self.directory) / (record["instance"] + ".json")).stat().st_mode & 0o777, 0o600)
        sweep = registry.Registry(self.directory)
        try:
            sweep.sweep()
        finally:
            sweep.close()
        self.assertEqual(len(registry.discover(self.directory)), 12)
        workers[0].kill()
        workers[0].communicate(timeout=5)
        self.await_rows(11)
        replacement = self.spawn(WORKER, self.directory)
        self.await_rows(12)
        self.assertEqual(len(list(Path(self.directory).iterdir())), 12)
        replacement.communicate(b"exit\n", timeout=5)
        self.assertEqual(len(list(Path(self.directory).iterdir())), 11)

    def test_live_record_lock_never_blocks_start_or_discovery(self):
        first = self.spawn(WORKER, self.directory)
        self.await_rows(1)
        started = time.monotonic()
        for _ in range(20):
            self.assertEqual(len(registry.discover(self.directory)), 1)
        self.assertLess(time.monotonic() - started, 2)
        self.spawn(WORKER, self.directory)
        self.await_rows(2)
        first.kill()
        first.communicate(timeout=5)
        self.await_rows(1)
        self.assertEqual(len(list(Path(self.directory).iterdir())), 1)

    def test_read_only_discovery_preserves_stale_files(self):
        registry.Registry(self.directory).close()
        stale = Path(self.directory) / ("3" * 32 + ".json")
        stale.write_text("stale")
        stale.chmod(0o600)
        before = stale.stat()
        self.assertEqual(registry.discover(self.directory, read_only=True), [])
        self.assertEqual(stale.read_text(), "stale")
        self.assertEqual(stale.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(registry.discover(self.directory), [])
        self.assertFalse(stale.exists())

    def test_socket_conflict_and_malicious_file_types(self):
        registry.Registry(self.directory).close()
        target = self.root / "victim"
        target.write_text("unchanged")
        (Path(self.directory) / ("1" * 32 + ".json")).symlink_to(target)
        os.mkfifo(str(Path(self.directory) / ("2" * 32 + ".json")))
        (Path(self.directory) / ("3" * 32 + ".json")).write_text("stale")
        with socket.socket() as busy, socket.socket() as candidate:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            with self.assertRaises(OSError):
                registry.bind_instance(candidate, "127.0.0.1", busy.getsockname()[1], self.directory)
        self.assertEqual(target.read_text(), "unchanged")
        self.assertFalse((Path(self.directory) / ("3" * 32 + ".json")).exists())
        self.assertEqual(registry.discover(self.directory), [])

    def test_ephemeral_bind_and_fixed_port_request_lifecycle(self):
        class ObservedSocket:
            def __init__(self):
                self.socket = socket.socket()
                self.calls = []
            def bind(self, address):
                self.calls.append(address)
                return self.socket.bind(address)
            def listen(self, count):
                self.socket.listen(count)
            def getsockname(self):
                return self.socket.getsockname()
        observed = ObservedSocket()
        try:
            lease = registry.bind_instance(observed, "127.0.0.1", directory=self.directory)
            self.assertEqual(observed.calls, [("127.0.0.1", 0)])
            self.assertEqual(lease.metadata["port"], observed.getsockname()[1])
            lease.close()
        finally:
            observed.socket.close()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            fixed = occupied.getsockname()[1]
            registry.configure_port(fixed, "/synthetic/project", self.directory)
            flags = list(Path(self.directory).glob("fixed-port-*.json"))
            self.assertEqual(len(flags), 1)
            self.assertEqual(flags[0].stat().st_mode & 0o777, 0o600)
            with socket.socket() as candidate:
                with self.assertRaises(OSError):
                    registry.bind_instance(candidate, "127.0.0.1", directory=self.directory,
                                           work_dir="/synthetic/project")
            # A directory-scoped flag must not affect another project.
            with socket.socket() as candidate:
                lease = registry.bind_instance(candidate, "127.0.0.1", directory=self.directory,
                                               work_dir="/other/project")
                lease.close()
        with socket.socket() as candidate:
            lease = registry.bind_instance(candidate, "127.0.0.1", directory=self.directory,
                                           work_dir="/synthetic/project")
            self.assertEqual(lease.metadata["port"], fixed)
            lease.close()
        registry.configure_port(None, "/synthetic/project", self.directory)
        self.assertFalse(list(Path(self.directory).glob("fixed-port-*.json")))
        # Account-wide compatibility request, with exact-scope precedence.
        registry.configure_port(12345, "", self.directory)
        registry.configure_port(12346, "/specific", self.directory)
        opened = registry.Registry(self.directory)
        try:
            self.assertEqual(registry.requested_port(opened, "/specific"), 12346)
            self.assertEqual(registry.requested_port(opened, "/other"), 12345)
        finally:
            opened.close()

    def test_permissive_scratch_and_malformed_flag_fail_closed(self):
        registry.Registry(self.directory).close()
        os.chmod(self.directory, 0o755)
        self.assertEqual(registry.discover(self.directory), [])
        os.chmod(self.directory, 0o700)
        registry.configure_port(12345, "", self.directory)
        flag = next(Path(self.directory).glob("fixed-port-*.json"))
        flag.write_text('{"port": "invalid", "work_dir": ""}')
        with socket.socket() as candidate:
            with self.assertRaises(ValueError):
                registry.bind_instance(candidate, "127.0.0.1", directory=self.directory)
        flag.unlink()
        target = self.root / "outside-flag"
        target.write_text('{"port": 12345, "work_dir": ""}')
        flag.symlink_to(target)
        with socket.socket() as candidate:
            with self.assertRaises(OSError):
                registry.bind_instance(candidate, "127.0.0.1", directory=self.directory)
        self.assertEqual(registry.discover(self.directory), [])

    def test_concurrent_fixed_port_starters_have_one_winner(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        registry.configure_port(port, "", self.directory)
        workers = [self.spawn(WORKER, self.directory) for _ in range(8)]
        rows = self.await_rows(1)
        deadline = time.monotonic() + 5
        while sum(p.poll() is None for p in workers) > 1 and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertEqual(sum(p.poll() is None for p in workers), 1)
        self.assertEqual(rows[0]["port"], port)
        self.assertEqual(len(list(Path(self.directory).glob("[0-9a-f]" * 32 + ".json"))), 1)

    def test_scratch_command_sets_private_flag_and_cleans_stale_records(self):
        root = self.root / "scratch root"
        root.mkdir(mode=0o700)
        command = [WORKER_PYTHON, str(RESOURCES / "ramic_registry.py"),
                   "--scratch-root", str(root), "--configure",
                   "--work-dir", "/project with spaces", "--port", "61234"]
        result = subprocess.run(command, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])
        opened = registry.Registry(root=str(root))
        try:
            self.assertEqual(registry.requested_port(opened, "/project with spaces"), 61234)
            self.assertEqual(os.fstat(opened.fd).st_mode & 0o777, 0o700)
            stale = Path(opened.anchor) / ("f" * 32 + ".json")
            stale.write_text("stale")
            result = subprocess.run(command, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(stale.exists())
        finally:
            opened.close()
        result = subprocess.run(command[:-2], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(root.rglob("fixed-port-*.json")))

    def test_directory_replacement_cannot_redirect_sweep_or_lease_cleanup(self):
        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / ("1" * 32 + ".json")
        victim.write_text("untouched")
        opened = registry.Registry(self.directory)
        os.rename(self.directory, self.directory + "-old")
        os.symlink(str(outside), self.directory)
        try:
            opened.sweep()
        finally:
            opened.close()
        self.assertEqual(victim.read_text(), "untouched")
        os.unlink(self.directory)
        os.rename(self.directory + "-old", self.directory)
        with socket.socket() as listener:
            lease = registry.bind_instance(listener, "127.0.0.1", directory=self.directory)
            replacement = outside / (lease.metadata["instance"] + ".json")
            replacement.write_text("preserve")
            os.rename(self.directory, self.directory + "-old")
            os.symlink(str(outside), self.directory)
            lease.close()
            self.assertEqual(replacement.read_text(), "preserve")

    def test_automatic_token_provisioning_failure_publishes_no_instance(self):
        private = self.root / "private"
        private.mkdir(mode=0o700)
        token_path = private / "bridge_token"
        token_path.write_text("invalid token")
        source = '''
import os, runpy, sys
sys.path.insert(0, sys.argv[1])
os.environ['RB_TOKEN_PATH'] = sys.argv[2] + '/private/bridge_token'
os.environ['RB_AUTO_PORT'] = '1'
os.environ['RB_SCRATCH_ROOT'] = sys.argv[2] + '/registry'
version = '27' if sys.version_info[0] == 2 else '3'
path = sys.argv[1] + '/ramic_bridge_daemon_' + version + '.py'
sys.argv = [path, '127.0.0.1', '0']
runpy.run_path(path, run_name='__main__')
'''
        process = self.spawn(source, self.root)
        stdout, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(registry.discover(root=str(self.root / 'registry')), [])
        self.assertEqual(token_path.read_text(), 'invalid token')
        self.assertNotIn(('ab' * 32).encode(), stdout + stderr)

    def test_automatic_daemon_creates_secret_and_rejects_foreign_credentials(self):
        self.check_discovery_daemon()

    def test_discovery_fixed_port_uses_legacy_token_override(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            fixed = probe.getsockname()[1]
        self.check_discovery_daemon(fixed)

    def check_discovery_daemon(self, fixed=0):
        token_path = self.root / "private" / "bridge_token"
        override = self.root / "legacy-token"
        override.write_text("00" * 32)
        override.chmod(0o600)
        # Redirect only test storage. Run the real standalone daemon and
        # wire protocol; no Virtuoso process or existing credential is used.
        source = '''
import os, runpy, sys
sys.path.insert(0,sys.argv[1])
import ramic_registry
resources, directory, token, port, override = sys.argv[1:6]
os.environ['RB_AUTO_PORT'] = '1'
os.environ['RB_TOKEN_PATH'] = override if int(port) else token
original = ramic_registry.bind_instance
def bind(*args, **kwargs):
    kwargs['directory'] = directory
    return original(*args, **kwargs)
ramic_registry.bind_instance = bind
os.environ['RB_VIRTUOSO_PID'] = str(os.getpid())
os.environ.pop('RB_VIRTUOSO_BOOT_ID', None)
os.environ['RB_VIRTUOSO_WORK_DIR'] = '/synthetic/project'
version = '27' if sys.version_info[0] == 2 else '3'
sys.argv = [resources + '/ramic_bridge_daemon_' + version + '.py', '127.0.0.1', port]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        process = self.spawn(source, self.directory, token_path, fixed, override)
        record = self.await_rows(1)[0]
        if fixed:
            self.assertEqual(record["port"], fixed)
        active_path = override if fixed else token_path
        token = active_path.read_text().strip()
        self.assertEqual(len(token), 64)
        self.assertEqual(active_path.stat().st_mode & 0o777, 0o600)
        if fixed:
            self.assertEqual(token, "00" * 32)
            self.assertFalse(token_path.exists())

        def mac(secret, *parts):
            frame = b"".join(str(len(p.encode())).encode() + b":" + p.encode() for p in parts)
            return hmac.new(secret.encode(), frame, hashlib.sha256).hexdigest()

        def request(payload):
            with socket.create_connection(("127.0.0.1", record["port"]), timeout=3) as channel:
                channel.sendall(json.dumps(payload).encode())
                channel.shutdown(socket.SHUT_WR)
                chunks = []
                while True:
                    chunk = channel.recv(65536)
                    if not chunk:
                        return b"".join(chunks)
                    chunks.append(chunk)

        nonce = os.urandom(16).hex()
        response = request(dict(op="hello", proto=1, nonce=nonce,
                                mac=mac(token, "vb1-hello", "1", nonce)))
        self.assertEqual(response[:1], b"\x02")
        body = response[65:].decode()
        self.assertEqual(response[1:65].decode(), mac(token, "vb1-response", nonce, "\x02", body))
        self.assertEqual(json.loads(body)["instance_record"], record)
        self.assertEqual(json.loads(body)["build"], "instance-discovery-3")
        for secret in ("11" * 32, "ff" * 32):
            nonce = os.urandom(16).hex()
            response = request(dict(proto=1, nonce=nonce, skill="1+1", timeout=1,
                                    mac=mac(secret, "vb1-request", "1", nonce, "1.000000", "1+1")))
            self.assertIn(b"AuthError", response)
        response = request(dict(skill="1+1", timeout=1))
        self.assertIn(b"AuthError", response)
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        self.assertNotIn(token.encode(), stdout + stderr)
        self.assertEqual(stdout, b"")  # no forged request reached the SKILL IPC lane


if __name__ == "__main__":
    unittest.main(verbosity=2)

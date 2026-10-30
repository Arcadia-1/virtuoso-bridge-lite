"""Linux full-daemon startup races; synthetic IPC, accounts and credentials.

Run with Python 3.9+. VB_TEST_WORKER_PYTHON may select actual Python 2.7.
Two-UID cases require root plus two disposable VB_TEST_ACCOUNT_USERS logins.
The harness never creates accounts or accesses their real home directories.
"""
import concurrent.futures
import hashlib
import hmac
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

if not sys.platform.startswith("linux"):
    print("native_daemon_concurrency: skipped; requires Linux UIDs, flock and /proc")
    raise SystemExit(0)

import pwd

RESOURCES = Path(sys.argv.pop(1)).resolve()
WORKER_PYTHON = os.environ.get("VB_TEST_WORKER_PYTHON", sys.executable)
ACCOUNT_NAMES = os.environ.get("VB_TEST_ACCOUNT_USERS", "").split(",")
ACCOUNTS = [pwd.getpwnam(name.strip()) for name in ACCOUNT_NAMES if name.strip()]
if ACCOUNTS and (os.geteuid() != 0 or len(ACCOUNTS) != 2
                 or len({a.pw_uid for a in ACCOUNTS}) != 2 or any(a.pw_uid == 0 for a in ACCOUNTS)):
    raise SystemExit("VB_TEST_ACCOUNT_USERS requires root and two distinct non-root fixture accounts")

WORKER = r'''
import os, runpy, sys
sys.path.insert(0, sys.argv[1])
sys.stdout.write('ready\n')
sys.stdout.flush()
assert sys.stdin.readline() == 'go\n'
os.environ['RB_VIRTUOSO_PID'] = str(os.getpid())
version = '27' if sys.version_info[0] == 2 else '3'
path = sys.argv[1] + '/ramic_bridge_daemon_' + version + '.py'
sys.argv = [path, '127.0.0.1', sys.argv[2]]
runpy.run_path(path, run_name='__main__')
'''


def mac(token, *parts):
    data = [part.encode() for part in parts]
    frame = b"".join(str(len(part)).encode() + b":" + part for part in data)
    return hmac.new(token.encode(), frame, hashlib.sha256).hexdigest()


def exchange(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall(json.dumps(payload).encode())
        connection.shutdown(socket.SHUT_WR)
        parts = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                return b"".join(parts)
            parts.append(chunk)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vb-startup-")
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.resources = self.root / "resources"
        self.resources.mkdir(mode=0o755)
        for path in RESOURCES.glob("*.py"):
            target = self.resources / path.name
            shutil.copyfile(path, target)
            target.chmod(0o644)
        self.project = self.root / "project"
        self.project.mkdir(mode=0o755)
        self.processes = []
        self.homes = {}

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.temp.cleanup()

    def account_options(self, account):
        if os.geteuid() != account.pw_uid:
            return dict(user=account.pw_uid, group=account.pw_gid, extra_groups=[])
        return {}

    def account_home(self, account):
        if account.pw_uid not in self.homes:
            home = self.root / ("account-%d" % account.pw_uid)
            home.mkdir(mode=0o700)
            if os.geteuid() == 0:
                os.chown(home, account.pw_uid, account.pw_gid)
            self.homes[account.pw_uid] = home
        return self.homes[account.pw_uid]

    def environment(self, account):
        home = self.account_home(account)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("RB_", "VB_", "PYTHON"))}
        env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", RB_AUTO_PORT="1",
                   RB_TOKEN_PATH=str(home / "private" / "bridge_token"),
                   RB_SCRATCH_ROOT=str(home / "registry"), RB_VIRTUOSO_WORK_DIR=str(self.project))
        return env

    def rows(self, account, read_only=True):
        source = ("import json,sys;sys.path.insert(0,sys.argv[1]);import ramic_registry;"
                  "print(json.dumps(ramic_registry.discover(read_only=%r)))" % read_only)
        result = subprocess.run([WORKER_PYTHON, "-B", "-c", source, str(self.resources)],
                                env=self.environment(account), cwd=self.project,
                                capture_output=True, timeout=5, **self.account_options(account))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def start(self, accounts, port=0):
        processes = []
        for account in accounts:
            process = subprocess.Popen([WORKER_PYTHON, "-B", "-u", "-c", WORKER,
                                        str(self.resources), str(port)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=self.environment(account), cwd=self.project,
                                       **self.account_options(account))
            self.processes.append(process)
            processes.append(process)
        # All workers reach the barrier before any imports/provisions a daemon.
        deadline = time.monotonic() + 15
        for process in processes:
            self.assertTrue(select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0],
                            "daemon worker did not reach startup barrier")
            self.assertEqual(process.stdout.readline(), b"ready\n")
        for process in processes:
            process.stdin.write(b"go\n")
            process.stdin.flush()
        return processes

    def await_rows(self, accounts, count):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rows = [row for account in accounts for row in self.rows(account)]
            if len(rows) == count:
                return rows
            time.sleep(0.03)
        self.fail("full-daemon startup did not publish %d records" % count)

    def hello(self, row, token):
        nonce = os.urandom(16).hex()
        reply = exchange(row["port"], dict(proto=1, op="hello", nonce=nonce,
                         mac=mac(token, "vb1-hello", "1", nonce)))
        self.assertEqual(reply[:1], b"\x02")
        body = reply[65:].decode()
        self.assertEqual(reply[1:65].decode(), mac(token, "vb1-response", nonce, "\x02", body))
        caps = json.loads(body)
        self.assertEqual(caps["instance_record"], row)
        self.assertEqual(caps["instance_bound_requests"], 1)
        return caps

    def execute_with_synthetic_ciw(self, row, token, caps):
        process = next(p for p in self.processes if p.pid == row["daemon_pid"])
        nonce = os.urandom(16).hex()
        request = dict(op="execute", proto=1, nonce=nonce, skill="1+1", timeout=2,
                       daemon_instance=caps["daemon_instance"])
        request["mac"] = mac(token, "vb1-execute", "1", nonce,
                             caps["daemon_instance"], "2.000000", "1+1")
        wrong = dict(request, daemon_instance="0" * 32)
        wrong["mac"] = mac(token, "vb1-execute", "1", nonce, "0" * 32, "2.000000", "1+1")
        self.assertIn(b"daemon_instance mismatch", exchange(row["port"], wrong))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
            reply = worker.submit(exchange, row["port"], request)
            self.assertTrue(select.select([process.stdout], [], [], 2)[0], "accepted request never reached IPC")
            self.assertIn(b"1+1", os.read(process.stdout.fileno(), 65536))
            process.stdin.write(b"\x022\x1e")
            process.stdin.flush()
            response = reply.result(timeout=3)
        self.assertEqual(response, b"\x02" + mac(token, "vb1-response", nonce, "\x02", "2").encode() + b"2")
        self.assertIn(b"replayed", exchange(row["port"], request))

    def exercise(self, accounts, fixed=False):
        workers = accounts * 3
        port = 0
        if fixed:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
        processes = self.start(workers, port)
        rows = self.await_rows(accounts, 1 if fixed else len(workers))
        self.assertEqual(len({row["port"] for row in rows}), len(rows))
        self.assertEqual(len({row["instance"] for row in rows}), len(rows))
        tokens = {}
        for account in accounts:
            token_path = self.account_home(account) / "private" / "bridge_token"
            # Even fixed-port losers provision before binding their socket.
            deadline = time.monotonic() + 5
            while not token_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            tokens[account.pw_uid] = token_path.read_text().strip()
            self.assertEqual(token_path.stat().st_uid, account.pw_uid)
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(token_path.parent.stat().st_mode & 0o777, 0o700)
            own = self.rows(account)
            self.assertTrue(all(r["uid"] == account.pw_uid and r["user"] == account.pw_name for r in own))
            self.assertTrue(all(r["work_dir"] == str(self.project) for r in own))
            for other in accounts:
                if other.pw_uid != account.pw_uid:
                    foreign = self.account_home(other) / "private" / "bridge_token"
                    denied = subprocess.run([WORKER_PYTHON, "-c", "import sys;open(sys.argv[1]).close()", str(foreign)],
                                            capture_output=True, timeout=5, env=self.environment(account),
                                            **self.account_options(account))
                    self.assertNotEqual(denied.returncode, 0, "another UID could open the private token")
        self.assertEqual(len(set(tokens.values())), len(accounts))
        for row in rows:
            token = tokens[row["uid"]]
            caps = self.hello(row, token)
            for other_token in list(tokens.values()) + ["ff" * 32]:
                if other_token == token:
                    continue
                nonce = os.urandom(16).hex()
                payload = dict(op="execute", proto=1, nonce=nonce, daemon_instance=caps["daemon_instance"],
                               skill="1+1", timeout=1)
                payload["mac"] = mac(other_token, "vb1-execute", "1", nonce,
                                     caps["daemon_instance"], "1.000000", "1+1")
                self.assertIn(b"AuthError", exchange(row["port"], payload))
            self.execute_with_synthetic_ciw(row, token, caps)
        if fixed:
            deadline = time.monotonic() + 5
            while sum(p.poll() is None for p in processes) > 1 and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertEqual(sum(p.poll() is None for p in processes), 1)
            self.assertEqual(rows[0]["port"], port)
            self.assertTrue(all(p.returncode in (None, 1) for p in processes))
        else:
            # A killed account's stale record must not affect any live peer.
            victim = rows[0]
            process = next(p for p in processes if p.pid == victim["daemon_pid"])
            process.kill()
            stdout, _ = process.communicate(timeout=5)
            self.assertEqual(stdout, b"", "rejected requests reached the IPC lane")
            owner = next(a for a in accounts if a.pw_uid == victim["uid"])
            stale = self.account_home(owner) / "registry" / "runs" / (victim["instance"] + ".json")
            self.assertTrue(stale.exists())
            self.assertNotIn(victim, self.rows(owner))
            self.assertTrue(stale.exists(), "read-only discovery removed evidence")
            self.rows(owner, read_only=False)
            self.assertFalse(stale.exists())
            survivors = self.await_rows(accounts, len(rows) - 1)
            for row in survivors:
                self.hello(row, tokens[row["uid"]])

    def test_same_account_full_daemon_startup(self):
        self.exercise([ACCOUNTS[0] if ACCOUNTS else pwd.getpwuid(os.geteuid())])

    def test_same_account_fixed_port_has_one_winner(self):
        self.exercise([ACCOUNTS[0] if ACCOUNTS else pwd.getpwuid(os.geteuid())], fixed=True)

    @unittest.skipUnless(ACCOUNTS, "set VB_TEST_ACCOUNT_USERS and run as root for actual two-UID coverage")
    def test_different_accounts_concurrently_start_in_one_project(self):
        self.exercise(ACCOUNTS)

    @unittest.skipUnless(ACCOUNTS, "set VB_TEST_ACCOUNT_USERS and run as root for actual two-UID coverage")
    def test_different_accounts_compete_for_fixed_port(self):
        self.exercise(ACCOUNTS, fixed=True)

    @unittest.skipUnless(ACCOUNTS, "set VB_TEST_ACCOUNT_USERS and run as root for actual two-UID coverage")
    def test_foreign_precreated_paths_are_rejected_by_real_accounts(self):
        victim, attacker = ACCOUNTS
        for operation in ("registry", "publish"):
            for kind in ("root", "ancestor"):
                with self.subTest(operation=operation, kind=kind):
                    planted = self.root / (operation + "-" + kind)
                    planted.mkdir(mode=0o777)
                    planted.chmod(0o777)
                    os.chown(planted, attacker.pw_uid, attacker.pw_gid)
                    target = planted
                    if kind == "ancestor":
                        target = planted / "victim-private"
                        target.mkdir(mode=0o700)
                        os.chown(target, victim.pw_uid, victim.pw_gid)
                    source = ("import sys;sys.path.insert(0,sys.argv[1]);"
                              "import ramic_registry,ramic_deploy;")
                    source += ("ramic_registry.Registry(root=sys.argv[2]).close()" if operation == "registry" else
                               "ramic_deploy.publish(sys.argv[2]+'/daemon', {'code.py':'trusted'})")
                    result = subprocess.run([WORKER_PYTHON, "-B", "-c", source, str(self.resources), str(target)],
                                            env=self.environment(victim), cwd=self.project, capture_output=True,
                                            timeout=5, **self.account_options(victim))
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(b"ValueError", result.stderr)
                    self.assertEqual(list(target.iterdir()), [])
                    self.assertEqual(planted.stat().st_uid, attacker.pw_uid)
                    self.assertEqual(planted.stat().st_mode & 0o777, 0o777)


if __name__ == "__main__":
    unittest.main(verbosity=2)

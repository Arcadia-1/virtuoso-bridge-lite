"""Exercise the shipped daemons with an owner and independently held IPC pipes."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest

from virtuoso_bridge import daemon_auth


RESOURCES = (Path(__file__).resolve().parents[1] / "src" / "virtuoso_bridge"
             / "virtuoso" / "basic" / "resources")
TOKEN = "ab" * 32


@pytest.mark.skipif(sys.platform != "linux", reason="Linux owner process lifecycle")
@pytest.mark.parametrize("version,python", [
    ("3", sys.executable),
    ("27", shutil.which("python2.7")),
])
@pytest.mark.parametrize("state", ["idle", "receiving", "waiting_for_skill"])
def test_daemon_exits_when_owner_dies_with_ipc_pipes_still_open(tmp_path, version, python, state):
    if python is None:
        pytest.skip("Python 2.7 is unavailable")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token_path = tmp_path / "token"
    token_path.write_text(TOKEN)
    # The test holds the daemon pipes independently, like a surviving
    # cdsServIpc. Killing the owner must work without pipe EOF or client input.
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    daemon = subprocess.Popen(
        [python, str(RESOURCES / ("ramic_bridge_daemon_%s.py" % version)), "127.0.0.1", str(port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, RB_TOKEN_PATH=str(token_path), RB_VIRTUOSO_PID=str(owner.pid),
                 RB_VIRTUOSO_BOOT_ID=Path("/proc/sys/kernel/random/boot_id").read_text().strip()),
    )
    conn = None
    try:
        deadline = time.monotonic() + 5
        while True:
            assert daemon.poll() is None, daemon.stderr.read().decode()
            try:
                conn = socket.create_connection(("127.0.0.1", port), timeout=0.1)
                break
            except OSError:
                assert time.monotonic() < deadline, "daemon did not listen"
                time.sleep(0.02)
        if state == "idle":
            conn.close()
            conn = None
        elif state == "receiving":
            conn.sendall(b'{"skill":')  # Keep recv() blocked on a partial request.
        else:
            nonce = "cd" * 16
            request = dict(proto=1, nonce=nonce, skill="1+1", timeout=60.0)
            request["mac"] = daemon_auth.request_mac(
                TOKEN, nonce=nonce, skill="1+1", timeout=60.0,
            )
            conn.sendall(json.dumps(request).encode())
            conn.shutdown(socket.SHUT_WR)
            import select
            assert select.select([daemon.stdout], [], [], 3)[0], "SKILL request not dispatched"
            assert b"1+1" in daemon.stdout.readline()
        assert daemon.poll() is None
        owner.kill()
        # Do not reap owner yet: a zombie must count as dead, not keep the
        # daemon alive just because kill(pid, 0) would still succeed.
        assert daemon.wait(timeout=3) == 0
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        if conn:
            conn.close()
        for process in (daemon, owner):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        for stream in (daemon.stdin, daemon.stdout, daemon.stderr):
            stream.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux owner process lifecycle")
@pytest.mark.parametrize("version,python", [
    ("3", sys.executable), ("27", shutil.which("python2.7")),
])
@pytest.mark.parametrize("owner_kind", ["absent", "collision", "unknown_host"])
def test_foreign_owner_never_uses_local_pid(tmp_path, version, python, owner_kind):
    if python is None:
        pytest.skip("Python 2.7 is unavailable")
    token_path = tmp_path / "token"
    token_path.write_text(TOKEN)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # A foreign PID can be missing locally or collide with an unrelated process.
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pid = (int(Path("/proc/sys/kernel/pid_max").read_text()) + 1
           if owner_kind == "absent" else bystander.pid)
    env = dict(os.environ, RB_TOKEN_PATH=str(token_path), RB_VIRTUOSO_PID=str(pid),
               RB_VIRTUOSO_BOOT_ID="foreign-boot")
    if owner_kind == "unknown_host":
        env.pop("RB_VIRTUOSO_BOOT_ID")
    daemon = subprocess.Popen(
        [python, str(RESOURCES / ("ramic_bridge_daemon_%s.py" % version)), "127.0.0.1", str(port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            assert daemon.poll() is None, "foreign owner prevented daemon startup"
            try:
                conn = socket.create_connection(("127.0.0.1", port), timeout=0.1)
                break
            except OSError:
                assert time.monotonic() < deadline, "daemon did not listen"
                time.sleep(0.02)
        with conn:
            conn.settimeout(3)
            nonce = "ef" * 16
            request = dict(proto=1, nonce=nonce, skill="1+1", timeout=0.1)
            request["mac"] = daemon_auth.request_mac(
                TOKEN, nonce=nonce, skill="1+1", timeout=0.1,
            )
            conn.sendall(json.dumps(request).encode())
            conn.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                response += chunk
            assert b"watchdog fired" in response
        time.sleep(0.1)
        assert bystander.poll() is None, "watchdog signalled an unrelated local process"
        bystander.kill()
        bystander.wait(timeout=3)
        # A local PID's death must not end a remote owner's daemon either.
        time.sleep(0.6)
        assert daemon.poll() is None
    finally:
        for process in (daemon, bystander):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        for stream in (daemon.stdin, daemon.stdout, daemon.stderr):
            stream.close()

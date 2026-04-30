#!/usr/bin/env python3
"""Minimal SKILL bridge daemon.

Spawned automatically by my_bridge.il via ipcBeginProcess().
Bridges TCP clients <-> Virtuoso stdin/stdout IPC channel.

Protocol (Virtuoso side):
  - success: \x02 <result> \x1e
  - error:   \x15 <message> \x1e
"""

import sys
import os
import socket
import fcntl
import time
import errno
import threading

HOST = sys.argv[1]
PORT = int(sys.argv[2])

# Make stdin non-blocking so we can poll for Virtuoso's response
# without blocking the process when Virtuoso is slow.
flags = fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETFL)
fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, flags | os.O_NONBLOCK)

# ipcBeginProcess spawns us as: virtuoso -> sh -> python (this process).
# When Virtuoso exits, children are reparented to init rather than killed,
# so we watch the grandparent PID and exit when it disappears.
def _get_virtuoso_pid() -> int:
    parent = int(open("/proc/self/stat").read().split()[3])
    return int(open(f"/proc/{parent}/stat").read().split()[3])

def _watchdog(virtuoso_pid: int, interval: float = 2.0):
    while True:
        time.sleep(interval)
        try:
            os.kill(virtuoso_pid, 0)  # signal 0: just check existence
        except ProcessLookupError:
            sys.stderr.write("[my_daemon] Virtuoso exited — shutting down\n")
            sys.stderr.flush()
            os._exit(0)

virtuoso_pid = _get_virtuoso_pid()
threading.Thread(target=_watchdog, args=(virtuoso_pid,), daemon=True).start()


def ask_virtuoso(skill_code: str) -> bytes:
    """Send SKILL to Virtuoso and read back the delimited response."""
    # Virtuoso's ipcBeginProcess reads our stdout as IPC input.
    sys.stdout.write(skill_code + "\n")
    sys.stdout.flush()

    # Read back: first byte is \x02 (success) or \x15 (error),
    # content follows, terminated by \x1e (RS, decimal 30).
    buf = bytearray()
    while True:
        try:
            ch = sys.stdin.buffer.read(1)
            if ch:
                if ch[0] == 0x1e:  # end-of-message delimiter
                    break
                buf.extend(ch)
        except IOError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                time.sleep(0.001)
                continue
            raise
    return bytes(buf)


def serve():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(1)
        sys.stderr.write(f"[my_daemon] ready on {HOST}:{PORT}\n")
        sys.stderr.flush()

        while True:
            conn, addr = s.accept()
            try:
                # Read full request from client
                chunks = []
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                skill_code = b"".join(chunks).decode("utf-8").strip()

                # Forward to Virtuoso, get result
                result = ask_virtuoso(skill_code)

                # Send result back to client (includes \x02/\x15 prefix)
                conn.sendall(result)
            except Exception as e:
                sys.stderr.write(f"[my_daemon] error: {e}\n")
                sys.stderr.flush()
                try:
                    conn.sendall(f"\x15{e}".encode())
                except Exception:
                    pass
            finally:
                conn.close()


if __name__ == "__main__":
    serve()

#!/usr/bin/env python
"""RAMIC Bridge Daemon — minimal TCP-to-Virtuoso IPC relay.

Launched by Virtuoso's ipcBeginProcess(). Receives SKILL commands over TCP,
writes them to stdout (→ Virtuoso), reads results from stdin (← Virtuoso),
sends results back over TCP.

Usage (called by ramic_bridge.il, not manually):
    python ramic_daemon.py 127.0.0.1 65432
"""

import sys
import socket
import os
import fcntl
import json
import errno
import time

HOST = sys.argv[1]
PORT = int(sys.argv[2])

# Non-blocking stdin (Virtuoso's IPC pipe)
fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL,
            fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETFL) | os.O_NONBLOCK)

STX = b'\x02'  # start-of-result (success)
NAK = b'\x15'  # start-of-result (error)
RS  = b'\x1e'  # end-of-result


def read_result():
    """Read one delimited result from Virtuoso via stdin."""
    buf = bytearray()
    started = False
    while True:
        try:
            ch = sys.stdin.read(1)
            if not ch:
                time.sleep(0.001)
                continue
            if not started:
                if ch in (STX, NAK, '\x02', '\x15'):
                    started = True
                    buf.extend(ch.encode('latin1') if isinstance(ch, str) else ch)
                continue
            if ch in (RS, '\x1e'):
                break
            buf.extend(ch.encode('latin1') if isinstance(ch, str) else ch)
        except IOError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                time.sleep(0.001)
                continue
            raise
    return bytes(buf)


def handle(conn):
    """Handle one client request."""
    chunks = []
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    req = json.loads(b"".join(chunks))

    # Send SKILL to Virtuoso
    sys.stdout.write(req["skill"])
    sys.stdout.flush()

    # Read result
    result = read_result()
    conn.sendall(result)


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(1)
    while True:
        conn, _ = s.accept()
        try:
            handle(conn)
        except Exception as e:
            try:
                conn.sendall(('\x15' + str(e)).encode('utf-8'))
            except:
                pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except:
                pass
            conn.close()


if __name__ == "__main__":
    main()

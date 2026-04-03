#!/usr/bin/env python3
"""RAMIC Bridge Daemon - Virtuoso Skill Bridge Service (Python 3 Version)"""

import sys
import socket
import os
import fcntl
import json
import signal
import threading
import time
import errno
import traceback

HOST = sys.argv[1]
PORT = int(sys.argv[2])

timeout_flag = False

# Get Virtuoso's PID (grandparent: virtuoso -> sh -> this daemon)
def get_grandparent_pid():
    try:
        with open('/proc/self/stat', 'r') as f:
            parent_pid = int(f.read().split()[3])
        with open(f'/proc/{parent_pid}/stat', 'r') as f:
            return int(f.read().split()[3])
    except Exception:
        raise Exception("Failed to get Virtuoso PID")

virtuoso_pid = get_grandparent_pid()

# Set stdin to non-blocking, keep stdout blocking
stdin_fd = sys.stdin.buffer.raw.fileno()
stdin_fl = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
fcntl.fcntl(stdin_fd, fcntl.F_SETFL, stdin_fl | os.O_NONBLOCK)

stdout_fd = sys.stdout.buffer.raw.fileno()
stdout_fl = fcntl.fcntl(stdout_fd, fcntl.F_GETFL)
fcntl.fcntl(stdout_fd, fcntl.F_SETFL, stdout_fl & ~os.O_NONBLOCK)

watchdog_timer = None

def watchdog_callback():
    global timeout_flag
    if not timeout_flag:
        timeout_flag = True
        try:
            os.kill(virtuoso_pid, signal.SIGINT)
        except Exception:
            pass

def read_until_delimiter(start_ok=0x02, start_err=0x15, end=0x1e):
    """Read data from Virtuoso's stdout until specific delimiters are found."""
    result = bytearray()

    # Wait for start marker
    while True:
        try:
            ch = sys.stdin.buffer.read(1)
            if not ch:
                if timeout_flag:
                    return b"\x15TimeoutError"
                time.sleep(0.001)
                continue
            if ch[0] in (start_ok, start_err):
                result.extend(ch)
                break
        except IOError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                if timeout_flag:
                    return b"\x15TimeoutError"
                time.sleep(0.001)
                continue
            raise
        if timeout_flag:
            return b"\x15TimeoutError"

    # Read content until end marker
    while True:
        try:
            ch = sys.stdin.buffer.read(1)
            if not ch:
                if timeout_flag:
                    return b"\x15TimeoutError"
                time.sleep(0.001)
                continue
            if ch[0] == end:
                break
            result.extend(ch)
        except IOError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                if timeout_flag:
                    return b"\x15TimeoutError"
                time.sleep(0.001)
                continue
            raise
        if timeout_flag:
            return b"\x15TimeoutError"

    return result

def handle_external_connection(conn, addr):
    global watchdog_timer, timeout_flag

    try:
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        request_data = json.loads(data.decode("utf-8"))

        skill_code = request_data["skill"]
        timeout_seconds = request_data["timeout"]

        timeout_flag = False

        # Clear stdin buffer before writing
        while True:
            try:
                ch = sys.stdin.buffer.read(1)
                if not ch:
                    break
            except IOError:
                break

        sys.stdout.buffer.write(skill_code.encode("utf-8"))
        sys.stdout.buffer.flush()

        # Start watchdog timer
        watchdog_timer = threading.Timer(timeout_seconds, watchdog_callback)
        watchdog_timer.daemon = True
        watchdog_timer.start()

        returnData = read_until_delimiter()

        if not timeout_flag:
            timeout_flag = True
        watchdog_timer.cancel()

        conn.sendall(returnData)

    except json.JSONDecodeError as e:
        conn.sendall(f"\x15JSONDecodeError: {e}".encode("utf-8"))
    except Exception as e:
        traceback.print_exc()
        conn.sendall(f"\x15{e}".encode("utf-8"))
    finally:
        timeout_flag = True
        if watchdog_timer:
            watchdog_timer.cancel()
        conn.shutdown(socket.SHUT_RDWR)
        conn.close()

def start_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, PORT))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                sys.stderr.write(f"ERROR: Port {PORT} is already in use. Another daemon may be running.\n")
                sys.exit(1)
            raise
        s.listen(1)
        while True:
            conn, addr = s.accept()
            handle_external_connection(conn, addr)

if __name__ == "__main__":
    start_server()

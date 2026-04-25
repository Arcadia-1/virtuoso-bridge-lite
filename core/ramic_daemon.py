#!/usr/bin/env python
"""RAMIC Bridge Daemon — TCP-to-Virtuoso IPC relay with result file.

Launched by Virtuoso's ipcBeginProcess(). Receives SKILL commands over TCP
(port N), writes them to stdout (→ Virtuoso). Results are received via a
temp file written by the SKILL-side RBSendCallback, which avoids both the
unreliable ipcBeginProcess data handler re-entry (issue #37) and shell
injection risks from system() calls.

Usage (called by ramic_bridge.il, not manually):
    python ramic_daemon.py 127.0.0.1 65432
"""

import sys
import socket
import os
import json
import time
import re
import atexit

HOST = sys.argv[1]
PORT = int(sys.argv[2])

STX = b'\x02'  # start-of-result (success)
NAK = b'\x15'  # start-of-result (error)

# Result file: Virtuoso writes results here instead of via stdin pipe.
# Port+1 is used in the filename to match the SKILL-side convention.
CALLBACK_PORT = PORT + 1
_RESULT_FILE = "/tmp/.ramic_cb_{}".format(CALLBACK_PORT)
_DONE_FILE = "/tmp/.ramic_cb_{}.done".format(CALLBACK_PORT)


def _clear_result_files():
    """Remove stale result files before sending a new command."""
    for path in (_RESULT_FILE, _DONE_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _cleanup_on_exit():
    """Remove result files when daemon exits."""
    _clear_result_files()


atexit.register(_cleanup_on_exit)


def read_result(timeout=30):
    """Poll for result file written by Virtuoso's RBSendCallback.

    The SKILL side writes the result to _RESULT_FILE, then creates
    _DONE_FILE as a completion marker.  This two-phase approach prevents
    reading a partially-written data file.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(_DONE_FILE):
            try:
                with open(_RESULT_FILE) as f:
                    text = f.read().strip()
                _clear_result_files()
                if text.startswith("OK "):
                    return STX + text[3:].encode('utf-8')
                elif text.startswith("ERR "):
                    return NAK + text[4:].encode('utf-8')
                else:
                    return NAK + b"malformed callback: " + text.encode('utf-8')
            except IOError:
                pass
        time.sleep(0.01)
    return NAK + b"timeout waiting for Virtuoso callback"


_BLOCKED_FNS = re.compile(
    r'(?<!["\w])(shell|system|ipcBeginProcess|getShellEnvVar|sstGetUserName)\s*\(',
)


_SKIP_CHECK = os.environ.get("RB_UNSAFE", "").lower() in ("1", "true", "yes")

if _SKIP_CHECK:
    print("[RAMIC] WARNING: RB_UNSAFE is enabled — SKILL safety checks are disabled",
          file=sys.stderr, flush=True)


def _check_skill(skill: str) -> None:
    """Reject SKILL code that calls dangerous shell-access functions.
    Disable with environment variable RB_UNSAFE=1."""
    if _SKIP_CHECK:
        return
    # Strip string literals so we don't false-positive on quoted names.
    stripped = re.sub(r'"[^"]*"', '""', skill)
    m = _BLOCKED_FNS.search(stripped)
    if m:
        raise ValueError(f"Blocked SKILL function: {m.group(1)!r}")


def handle(conn):
    """Handle one client request."""
    chunks = []
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    req = json.loads(b"".join(chunks))

    # Flatten multi-line SKILL into a single line so that Virtuoso's
    # ipcBeginProcess (which fires the data handler per line) receives
    # the entire expression in one callback.  Strip ; comments first
    # because they would swallow everything after them on the joined line.
    # The regex skips semicolons inside "quoted strings".
    skill = re.sub(r'"[^"]*"|;[^\n]*', lambda m: m.group() if m.group().startswith('"') else ' ', req["skill"])
    skill = ' '.join(skill.split())                  # collapse whitespace

    _check_skill(skill)

    # Clear stale result files before sending the new command
    _clear_result_files()

    # Send SKILL to Virtuoso (newline required — ipcBeginProcess is line-based)
    sys.stdout.write(skill + '\n')
    sys.stdout.flush()

    # Read result via file polling (timeout from client request, default 30s)
    result = read_result(timeout=req.get("timeout", 30))
    conn.sendall(result)


def main():
    _clear_result_files()  # clean slate on startup
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
